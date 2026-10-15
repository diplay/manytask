from types import SimpleNamespace
from unittest.mock import MagicMock

import gitlab
import pytest

from manytask.glab import GitLabApi, GitLabApiException, Student


def missing():
    return gitlab.GitlabGetError("Not found", response_code=404)


@pytest.fixture
def provisioning(monkeypatch):
    api = GitLabApi.__new__(GitLabApi)
    api._gitlab = MagicMock()
    api._course_group = "course"
    api._course_public_repo = "course/public"
    api._course_students_group = "course/students"
    api._default_branch = "main"
    api._FORK_WAIT_ATTEMPTS = 3
    monkeypatch.setattr("manytask.glab.time.sleep", lambda _: None)
    student = Student(id=42, username="new-student", name="Test Student")
    project = MagicMock()
    project.id = 12
    project.path_with_namespace = "course/students/new-student"
    project.default_branch = "main"
    project.attributes = {
        "default_branch": "main",
        "import_status": "finished",
        "namespace": {"id": 5},
        "forked_from_project": {"id": 10},
    }
    source = MagicMock(id=10)
    source.attributes = {"visibility": "public"}
    api._get_group_by_name = MagicMock(return_value=SimpleNamespace(id=5))
    api._get_project_by_name = MagicMock(return_value=source)
    state = {"exists": False, "protection": None, "direct": None, "inherited": None, "events": []}

    def get_project(key):
        if isinstance(key, str) and not state["exists"]:
            raise missing()
        return project

    def fork(_settings):
        state["exists"] = True
        state["events"].append("fork")
        return SimpleNamespace(id=project.id)

    def update_project(_id, settings):
        project.attributes.update(settings)
        state["events"].append("settings")
        return project

    def get_protection(_name):
        if state["protection"] is None:
            raise missing()
        return state["protection"]

    def protect(settings):
        state["events"].append("protect")
        protection = MagicMock()
        protection.attributes = {
            "push_access_levels": [{"access_level": settings["push_access_level"]}],
            "merge_access_levels": [{"access_level": settings["merge_access_level"]}],
            "allow_force_push": settings["allow_force_push"],
        }
        protection.delete.side_effect = lambda: state.update(protection=None)
        state["protection"] = protection
        return protection

    def get_member(_id, *, inherited=False):
        level = state["direct"]
        if inherited and state["inherited"] is not None:
            level = max(level or 0, state["inherited"])
        if level is None:
            raise missing()
        return SimpleNamespace(access_level=level)

    def grant(settings):
        state["events"].append("grant")
        state["direct"] = settings["access_level"]

    api._gitlab.projects.get.side_effect = get_project
    api._gitlab.projects.update.side_effect = update_project
    source.forks.create.side_effect = fork
    project.protectedbranches.get.side_effect = get_protection
    project.protectedbranches.create.side_effect = protect
    project.members.get.side_effect = get_member
    project.members_all.get.side_effect = lambda user_id: get_member(user_id, inherited=True)
    project.members.create.side_effect = grant
    project.members.update.side_effect = lambda _id, settings: grant(settings)
    return SimpleNamespace(api=api, student=student, project=project, source=source, state=state)


def test_new_fork_is_configured_and_verified_before_developer_access(provisioning):
    p = provisioning
    p.api.create_project(p.student)
    assert p.state["events"] == ["fork", "settings", "protect", "grant"]
    assert p.api.check_project_exists(p.student)
    p.source.forks.create.assert_called_once_with(
        {
            "name": "new-student",
            "path": "new-student",
            "namespace_id": 5,
            "visibility": "private",
            "mr_default_target_self": True,
        }
    )
    p.project.protectedbranches.list.assert_not_called()


def test_retry_reuses_fork_and_does_not_rewrite_matching_settings(provisioning):
    p = provisioning
    p.api.create_project(p.student)
    p.state["events"].clear()
    p.api.create_project(p.student)
    assert p.state["events"] == []
    assert p.source.forks.create.call_count == 1


def test_partial_setup_returns_to_creation_form_and_can_be_retried(provisioning):
    p = provisioning
    p.api._gitlab.projects.update.side_effect = gitlab.GitlabUpdateError("Denied", response_code=403)
    with pytest.raises(gitlab.GitlabUpdateError):
        p.api.create_project(p.student)
    assert not p.api.check_project_exists(p.student)
    p.project.members.create.assert_not_called()
    p.api._gitlab.projects.update.side_effect = lambda _id, settings: p.project.attributes.update(settings)
    p.api.create_project(p.student)
    assert p.api.check_project_exists(p.student)
    assert p.source.forks.create.call_count == 1


@pytest.mark.parametrize("status", ["scheduled", "started", "failed"])
def test_pending_or_failed_import_never_grants_access(provisioning, status):
    p = provisioning
    p.project.attributes["import_status"] = status
    with pytest.raises(GitLabApiException):
        p.api.create_project(p.student)
    p.api._gitlab.projects.update.assert_not_called()
    p.project.members.create.assert_not_called()


def test_waits_for_import_and_default_branch(provisioning):
    p = provisioning
    p.project.branches.get.side_effect = [missing(), SimpleNamespace(name="main")]
    p.api.create_project(p.student)
    assert p.project.branches.get.call_count == 2
    assert p.api.check_project_exists(p.student)


@pytest.mark.parametrize("field,value", [("forked_from_project", {"id": 999}), ("namespace", {"id": 999})])
def test_unrelated_existing_project_is_not_modified(provisioning, field, value):
    p = provisioning
    p.state["exists"] = True
    p.project.attributes[field] = value
    with pytest.raises(GitLabApiException, match="does not match"):
        p.api.create_project(p.student)
    p.api._gitlab.projects.update.assert_not_called()
    p.project.members.create.assert_not_called()


def test_unverified_settings_never_grant_access(provisioning):
    p = provisioning
    p.api._gitlab.projects.update.side_effect = None
    with pytest.raises(GitLabApiException, match="settings"):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()


def test_branch_setup_failure_never_grants_access(provisioning):
    p = provisioning
    p.project.protectedbranches.create.side_effect = gitlab.GitlabCreateError("Denied", response_code=403)
    with pytest.raises(gitlab.GitlabCreateError):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()


def test_replaces_only_default_branch_rule_when_role_differs(provisioning):
    p = provisioning
    p.api.create_project(p.student)
    protection = p.state["protection"]
    protection.attributes["push_access_levels"] = [{"access_level": 40}]
    p.api.create_project(p.student)
    protection.delete.assert_called_once()
    p.project.protectedbranches.list.assert_not_called()
    assert p.api.check_project_exists(p.student)


def test_inherited_protection_is_not_deleted(provisioning):
    p = provisioning
    p.api.create_project(p.student)
    protection = p.state["protection"]
    protection.attributes.update(inherited=True, allow_force_push=True)
    with pytest.raises(GitLabApiException, match="Inherited"):
        p.api.create_project(p.student)
    protection.delete.assert_not_called()


def test_elevated_inherited_access_is_rejected(provisioning):
    p = provisioning
    p.state["inherited"] = 40
    with pytest.raises(GitLabApiException, match="elevated"):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()


def test_membership_failure_is_not_silently_accepted(provisioning):
    p = provisioning
    p.project.members.create.side_effect = gitlab.GitlabCreateError("Denied", response_code=403)
    with pytest.raises(gitlab.GitlabCreateError):
        p.api.create_project(p.student)
    assert not p.api.check_project_exists(p.student)


def test_missing_project_does_not_mask_api_errors(provisioning):
    p = provisioning
    p.api._gitlab.projects.get.side_effect = gitlab.GitlabGetError("Denied", response_code=403)
    with pytest.raises(gitlab.GitlabGetError):
        p.api.check_project_exists(p.student)
    p.source.forks.create.assert_not_called()


def test_concurrent_fork_creation_reuses_existing_project(provisioning):
    p = provisioning

    def conflict(_settings):
        p.state["exists"] = True
        raise gitlab.GitlabCreateError("Already exists", response_code=409)

    p.source.forks.create.side_effect = conflict
    p.api.create_project(p.student)
    assert p.api.check_project_exists(p.student)
    p.source.forks.create.assert_called_once()


def test_concurrent_membership_creation_is_verified(provisioning):
    p = provisioning

    def conflict(settings):
        p.state["direct"] = settings["access_level"]
        raise gitlab.GitlabCreateError("Already exists", response_code=409)

    p.project.members.create.side_effect = conflict
    p.api.create_project(p.student)
    assert p.api.check_project_exists(p.student)


def test_teacher_access_is_preserved(provisioning):
    p = provisioning
    p.student.course_admin = True
    p.state["inherited"] = 50
    p.api.create_project(p.student)
    assert p.api.check_project_exists(p.student)
    p.project.members.create.assert_not_called()


def test_wrong_default_branch_is_rejected(provisioning):
    p = provisioning
    p.project.attributes["default_branch"] = "master"
    with pytest.raises(GitLabApiException, match="default branch"):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()


def test_missing_default_branch_waits_without_granting_access(provisioning):
    p = provisioning
    p.project.attributes.pop("default_branch")
    with pytest.raises(GitLabApiException, match="not ready"):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()


def test_private_starter_gets_only_reporter_before_student_access(provisioning):
    p = provisioning
    p.source.attributes["visibility"] = "private"
    p.source.members_all.get.side_effect = [missing(), SimpleNamespace(access_level=20)]
    p.source.members.get.side_effect = missing()
    p.api.create_project(p.student)
    p.source.members.create.assert_called_once_with({"user_id": 42, "access_level": 20})
    p.project.members.create.assert_called_once_with({"user_id": 42, "access_level": 30})


def test_private_starter_failure_blocks_student_access(provisioning):
    p = provisioning
    p.source.attributes["visibility"] = "private"
    p.source.members_all.get.side_effect = missing()
    p.source.members.get.side_effect = missing()
    p.source.members.create.side_effect = gitlab.GitlabCreateError("Denied", response_code=403)
    with pytest.raises(gitlab.GitlabCreateError):
        p.api.create_project(p.student)
    p.project.members.create.assert_not_called()
