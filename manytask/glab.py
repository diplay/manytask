from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import gitlab
import gitlab.const
import gitlab.v4.objects
import requests

logger = logging.getLogger(__name__)


@dataclass
class User:
    username: str
    firstname: str
    lastname: str
    email: str
    password: str

    def __repr__(self) -> str:
        return f"User(username={self.username})"


@dataclass
class Student:
    id: int
    username: str
    name: str
    course_admin: bool = False
    repo: str | None = field(default=None)

    def __repr__(self) -> str:
        return f"Student(username={self.username})"


class GitLabApiException(gitlab.GitlabError):
    pass


class GitLabApi:
    _FORK_WAIT_ATTEMPTS = 30
    _FORK_POLL_INTERVAL = 1

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        course_group: str,
        course_public_repo: str,
        course_students_group: str,
        default_branch: str = "main",
        *,
        dry_run: bool = False,
    ):
        """
        :param base_url:
        :param admin_token:
        :param course_group:
        :param course_public_repo:
        :param course_students_group:
        :param default_branch:
        """
        self.dry_run = dry_run

        self.base_url = base_url
        self._gitlab = gitlab.Gitlab(self.base_url, private_token=admin_token)

        self._course_group = course_group
        self._course_public_repo = course_public_repo
        self._course_students_group = course_students_group

        self._default_branch = default_branch

        # test can find groups and repos
        if self._course_group:
            self._get_group_by_name(self._course_group)
        if self._course_public_repo:
            self._get_project_by_name(self._course_public_repo)
        if self._course_students_group:
            self._get_group_by_name(self._course_students_group)

    def register_new_user(
        self,
        user: User,
    ) -> gitlab.v4.objects.User:
        """
        :param user:
        :return: returns this thing
        https://python-gitlab.readthedocs.io/en/stable/api/gitlab.v4.html#gitlab.v4.objects.User
        but the docs do not really help much. Grep the logs
        """

        logger.info(f"Creating user: {user}")
        # was invented to distinguish between different groups of users automatically by secret
        new_user = self._gitlab.users.create(
            {
                "email": user.email,
                "username": user.username,
                "name": user.firstname + " " + user.lastname,
                "external": False,
                "password": user.password,
                "skip_confirmation": True,
            }
        )
        logger.info(f"Gitlab user created {new_user}")

        return new_user  # type: ignore

    def _get_group_by_name(self, group_name: str) -> gitlab.v4.objects.Group:
        try:
            return self._gitlab.groups.get(group_name)
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
            raise GitLabApiException(f"Unable to find group {group_name}") from error

    def _get_project_by_name(self, project_name: str) -> gitlab.v4.objects.Project:
        short_project_name = project_name.split("/")[-1]

        try:
            return next(  # type: ignore
                project
                for project in self._gitlab.projects.list(get_all=True, search=short_project_name)
                if project.path_with_namespace == project_name
            )
        except StopIteration:
            raise RuntimeError(f"Unable to find project {project_name}")

    def create_public_repo(
        self,
    ) -> None:
        group = self._get_group_by_name(self._course_group)

        for project in self._gitlab.projects.list(get_all=True, search=self._course_public_repo):
            if project.path_with_namespace == self._course_public_repo:
                logger.info(f"Project {self._course_public_repo} already exists")
                return

        self._gitlab.projects.create(
            {
                "name": self._course_public_repo,
                "path": self._course_public_repo,
                "namespace_id": group.id,
                "visibility": "public",
                "shared_runners_enabled": True,
                "auto_devops_enabled": False,
                "initialize_with_readme": True,
            }
        )

    def create_students_group(
        self,
    ) -> None:
        for group in self._gitlab.groups.list(get_all=True, search=self._course_students_group):
            if group.name == self._course_students_group and group.full_name == self._course_students_group:
                logger.info(f"Group {self._course_students_group} already exists")
                return

        self._gitlab.groups.create(
            {
                "name": self._course_students_group,
                "path": self._course_students_group,
                "visibility": "private",
                "lfs_enabled": True,
                "shared_runners_enabled": True,
            }
        )

    def check_project_exists(
        self,
        student: Student,
    ) -> bool:
        project = self._find_student_project(student)
        if project is None or project.attributes.get("default_branch") != self._default_branch:
            return False
        if project.attributes.get("import_status") not in ("none", "finished"):
            return False
        if any(project.attributes.get(key) != value for key, value in self._student_project_settings().items()):
            return False
        try:
            protection = project.protectedbranches.get(self._default_branch)
            member = project.members_all.get(student.id)
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
            return False
        return self._default_branch_is_protected(protection) and (
            member.access_level == gitlab.const.AccessLevel.DEVELOPER
            or (student.course_admin and member.access_level >= gitlab.const.AccessLevel.MAINTAINER)
        )

    def _find_student_project(self, student: Student) -> gitlab.v4.objects.Project | None:
        path = f"{self._course_students_group}/{student.username}"
        try:
            project = self._gitlab.projects.get(path)
            if project.path_with_namespace != path:
                return None
            return project
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
            return None

    def _student_project_settings(self) -> dict[str, Any]:
        return {
            "visibility": "private",
            "ci_config_path": f".gitlab-ci.yml@{self._course_public_repo}",
            "ci_pipeline_variables_minimum_override_role": "no_one_allowed",
            "ci_allow_fork_pipelines_to_run_in_parent_project": False,
            "forking_access_level": "disabled",
            "shared_runners_enabled": False,
            "auto_devops_enabled": False,
            "mr_default_target_self": True,
        }

    def _wait_for_student_project(self, project_id: int) -> gitlab.v4.objects.Project:
        for attempt in range(self._FORK_WAIT_ATTEMPTS):
            project = self._gitlab.projects.get(project_id)
            if project.attributes.get("import_status") == "failed":
                raise GitLabApiException("Student repository import failed; retry after fixing the import")
            default_branch = project.attributes.get("default_branch")
            if project.attributes.get("import_status") in ("none", "finished") and default_branch:
                if default_branch != self._default_branch:
                    raise GitLabApiException(f"Student repository must use default branch {self._default_branch}")
                try:
                    project.branches.get(self._default_branch)
                    return project
                except gitlab.GitlabGetError as error:
                    if error.response_code != 404:
                        raise
            if attempt + 1 < self._FORK_WAIT_ATTEMPTS:
                time.sleep(self._FORK_POLL_INTERVAL)
        raise GitLabApiException("Student repository import is not ready yet; please retry")

    @staticmethod
    def _default_branch_is_protected(protection: gitlab.v4.objects.ProjectProtectedBranch) -> bool:
        attributes = protection.attributes
        for access_field, expected in (("push_access_levels", 30), ("merge_access_levels", 40)):
            levels = attributes.get(access_field, [])
            if len(levels) != 1 or levels[0].get("access_level") != expected:
                return False
            if any(
                levels[0].get(key) is not None for key in ("user_id", "group_id", "deploy_key_id", "member_role_id")
            ):
                return False
        return attributes.get("allow_force_push") is True

    def _protect_student_default_branch(self, project: gitlab.v4.objects.Project) -> None:
        try:
            protection = project.protectedbranches.get(self._default_branch)
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
        else:
            if self._default_branch_is_protected(protection):
                return
            # Role-level updates need a paid tier. Replace only this rule on GitLab Free.
            if protection.attributes.get("inherited"):
                raise GitLabApiException("Inherited default branch protection does not match the course policy")
            protection.delete()
        project.protectedbranches.create(
            {
                "name": self._default_branch,
                "push_access_level": gitlab.const.AccessLevel.DEVELOPER,
                "merge_access_level": gitlab.const.AccessLevel.MAINTAINER,
                "allow_force_push": True,
            }
        )
        if not self._default_branch_is_protected(project.protectedbranches.get(self._default_branch)):
            raise GitLabApiException("Unable to verify student default branch protection")

    def _grant_project_access(
        self,
        project: gitlab.v4.objects.Project,
        student: Student,
        access_level: int,
        *,
        allow_higher: bool = False,
    ) -> None:
        try:
            member = project.members_all.get(student.id)
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
        else:
            if member.access_level >= access_level and allow_higher:
                return
            if member.access_level > access_level:
                if student.course_admin:
                    return
                raise GitLabApiException("Student already has elevated project access; review group memberships")
        try:
            direct_member = project.members.get(student.id)
        except gitlab.GitlabGetError as error:
            if error.response_code != 404:
                raise
            try:
                project.members.create({"user_id": student.id, "access_level": access_level})
            except gitlab.GitlabCreateError as error:
                if error.response_code != 409:
                    raise
        else:
            if direct_member.access_level != access_level:
                project.members.update(student.id, {"access_level": access_level})
        actual_access = project.members_all.get(student.id).access_level
        if actual_access < access_level or (actual_access != access_level and not allow_higher):
            raise GitLabApiException("Unable to verify student project access")

    def create_project(
        self,
        student: Student,
    ) -> None:
        course_group = self._get_group_by_name(self._course_students_group)

        course_public_project = self._get_project_by_name(self._course_public_repo)
        project = self._find_student_project(student)
        if project is None:
            try:
                fork = course_public_project.forks.create(
                    {
                        "name": student.username,
                        "path": student.username,
                        "namespace_id": course_group.id,
                        "visibility": "private",
                        "mr_default_target_self": True,
                    }
                )
            except gitlab.GitlabCreateError as error:
                if error.response_code != 409:
                    raise
                project = self._find_student_project(student)
                if project is None:
                    raise
            else:
                project = self._gitlab.projects.get(fork.id)
        project = self._wait_for_student_project(project.id)
        if (
            project.path_with_namespace != f"{self._course_students_group}/{student.username}"
            or (project.attributes.get("namespace") or {}).get("id") != course_group.id
            or (project.attributes.get("forked_from_project") or {}).get("id") != course_public_project.id
        ):
            raise GitLabApiException("Existing repository does not match this student's course fork")
        settings = self._student_project_settings()
        updates = {key: value for key, value in settings.items() if project.attributes.get(key) != value}
        if updates:
            self._gitlab.projects.update(project.id, updates)
        project = self._gitlab.projects.get(project.id)
        if any(project.attributes.get(key) != value for key, value in settings.items()):
            raise GitLabApiException("Unable to verify student repository settings")
        self._protect_student_default_branch(project)
        if course_public_project.attributes.get("visibility") == "private":
            self._grant_project_access(
                course_public_project, student, gitlab.const.AccessLevel.REPORTER, allow_higher=True
            )
        self._grant_project_access(project, student, gitlab.const.AccessLevel.DEVELOPER)
        logger.info("Student repository ready: %s", project.path_with_namespace)

    def _check_is_course_admin(self, user_id: int) -> bool:
        try:
            admin_group = self._get_group_by_name(self._course_group)
            admin_group_member = admin_group.members_all.get(user_id)
        except Exception:
            return False

        if not admin_group_member:
            return False

        return True

    def _parse_user_to_student(
        self,
        user: dict[str, Any],
    ) -> Student:
        return Student(
            id=user["id"],
            username=user["username"],
            name=user["name"],
            repo=self.get_url_for_repo(user["username"]),
            course_admin=self._check_is_course_admin(user["id"]),
        )

    def get_students_by_username(
        self,
        username: str,
    ) -> list[Student]:
        users = self._gitlab.users.list(get_all=True, username=username)
        return [self._parse_user_to_student(user._attrs) for user in users]

    def get_student(
        self,
        user_id: int,
    ) -> Student:
        logger.info(f"Searching user {user_id}...")
        user = self._gitlab.users.get(user_id)
        logger.info(f'User found: "{user.username}"')
        return self._parse_user_to_student(user._attrs)

    def get_student_by_username(
        self,
        username: str,
    ) -> Student:
        potential_students = self.get_students_by_username(username)
        potential_students = [student for student in potential_students if student.username == username]
        if len(potential_students) == 0:
            raise GitLabApiException(f"No students found for username {username}")

        student = potential_students[0]
        logger.info(f'User found: "{student.username}"')
        return student

    def get_authenticated_student(
        self,
        oauth_token: str,
    ) -> Student:
        headers = {"Authorization": "Bearer " + oauth_token}
        response = requests.get(f"{self.base_url}/api/v4/user", headers=headers)
        response.raise_for_status()
        return self._parse_user_to_student(response.json())

    def get_url_for_task_base(self) -> str:
        return f"{self.base_url}/{self._course_public_repo}/blob/{self._default_branch}"

    def get_url_for_repo(
        self,
        username: str,
    ) -> str:
        return f"{self.base_url}/{self._course_students_group}/{username}"


def map_gitlab_user_to_student(
    gitlab_response: gitlab.v4.objects.User,
) -> Student:
    return Student(
        id=gitlab_response.id,
        username=gitlab_response.username,
        name=gitlab_response.name,
        repo=gitlab_response.web_url,
    )
