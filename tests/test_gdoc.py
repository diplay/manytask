from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cachelib import BaseCache

from manytask.gdoc import GoogleDocApi
from manytask.glab import Student


@pytest.fixture
def empty_google_doc_api():
    return GoogleDocApi(
        base_url="",
        gdoc_credentials={},
        public_worksheet_id="",
        public_scoreboard_sheet=0,
        cache=BaseCache(),
        testing=True,
    )


def test_get_stored_user(empty_google_doc_api):
    student1 = Student(id=0, username="user1", name="", course_admin=False)
    stored_user1 = empty_google_doc_api.get_stored_user(student1)

    assert stored_user1.username == "user1"
    assert not stored_user1.course_admin

    student2 = Student(id=0, username="user2", name="", course_admin=True)
    stored_user2 = empty_google_doc_api.get_stored_user(student2)

    assert stored_user2.username == "user2"
    assert not stored_user2.course_admin


def test_sync_stored_user(empty_google_doc_api):
    student1 = Student(id=0, username="user1", name="", course_admin=False)

    assert empty_google_doc_api.sync_stored_user(student1) == empty_google_doc_api.get_stored_user(student1)

    student2 = Student(id=0, username="user2", name="", course_admin=True)

    assert empty_google_doc_api.sync_stored_user(student2) == empty_google_doc_api.get_stored_user(student2)


@pytest.mark.parametrize("add_task", [False, True])
def test_sync_maximum_uses_percentage_denominator_cell(empty_google_doc_api, add_task):
    api = empty_google_doc_api
    api.ws = Mock()
    api.ws.row_values.return_value = [""] * 13 + ["existing"]
    tasks = [SimpleNamespace(name="existing", score=100)]
    if add_task:
        tasks.append(SimpleNamespace(name="new", score=50))
    deadlines = Mock()
    deadlines.max_score_started = sum(task.score for task in tasks)
    deadlines.get_groups.return_value = [SimpleNamespace(name="intro", tasks=tasks)]
    deadlines.get_tasks.return_value = tasks

    api.sync_columns(deadlines)

    updated = {(cell.row, cell.col): cell.value for cell in api.ws.update_cells.call_args.args[0]}
    assert updated[2, 6] == str(deadlines.max_score_started)
    assert (1, 6) not in updated
    assert all(row <= 3 for row, _ in updated)


def test_new_student_percentage_uses_canonical_maximum(empty_google_doc_api):
    api = empty_google_doc_api
    api.ws = Mock()
    api.ws.append_row.return_value = {"updates": {"updatedRange": "Grades!A4:G4"}}
    api._url = "https://gitlab.example.invalid"
    student = Student(id=1, username="student", name="Test Student")

    api._add_student_row(student)

    row = api.ws.append_row.call_args.kwargs["values"]
    assert row[6] == "=IFERROR(INDIRECT(ADDRESS(ROW(); 6)) / INDIRECT(ADDRESS(2; 6)); 0)"
