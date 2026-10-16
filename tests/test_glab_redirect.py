"""Rejoining after deletion must create a new fork, not reuse the renamed project."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from manytask.glab import GitLabApi, Student


def test_rejoin_creates_new_fork_when_old_path_redirects():
    api = GitLabApi.__new__(GitLabApi)
    api._gitlab = MagicMock()
    api._course_students_group = 'course/students'
    api._course_public_repo = 'course/public'
    api._default_branch = 'master'
    student = Student(id=7, username='student', name='Test Student')
    path = 'course/students/student'
    old = SimpleNamespace(id=10, path_with_namespace=path + '-deletion_scheduled-10')
    public = MagicMock()
    public.id = 1
    public.attributes = {'visibility': 'public'}
    public.forks.create.return_value.id = 20
    new = SimpleNamespace(
        id=20,
        path_with_namespace=path,
        attributes={**api._student_project_settings(), 'namespace': {'id': 3}, 'forked_from_project': {'id': 1}},
    )
    api._get_group_by_name = MagicMock(return_value=SimpleNamespace(id=3))
    api._get_project_by_name = MagicMock(return_value=public)
    api._gitlab.projects.get.side_effect = lambda project: old if project == path else new
    api._wait_for_student_project = MagicMock(side_effect=lambda project_id: old if project_id == 10 else new)
    api._protect_student_default_branch = MagicMock()
    api._grant_project_access = MagicMock()

    api.create_project(student)

    public.forks.create.assert_called_once_with({
        'name': 'student', 'path': 'student', 'namespace_id': 3,
        'visibility': 'private', 'mr_default_target_self': True,
    })
    api._wait_for_student_project.assert_called_once_with(20)
    api._protect_student_default_branch.assert_called_once_with(new)
    assert api._grant_project_access.call_args.args[0] is new
