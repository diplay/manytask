# GitLab.com with Google Sheets on 25.1

This branch retains the Google Sheets backend from release 25.1 and adds a
specific student-project policy for a course using its own GitLab Runner.
It does not restore Google Sheets support to later Manytask releases.

## Authentication and storage

Set `GITLAB_URL=https://gitlab.com` and `STORAGE=gsheets`. Configure the
existing `GDOC_*` settings for a new course spreadsheet. Keep credentials
outside Git and Docker images.

Create a GitLab OAuth application with the `read_user` scope and the course's
HTTPS `/login_finish` callback. Students use their existing GitLab.com
accounts, log in through OAuth, and enter the course registration code.
The server's `GITLAB_ADMIN_TOKEN` creates their forks; students do not need
permission to create projects in the course group. GitLab.com account
registration itself is handled by GitLab, not Manytask's self-managed
GitLab signup endpoint.

The server token needs API access to the starter project, the students'
subgroup, project settings, branch protection, and project memberships.
Configure the existing `GITLAB_COURSE_*` variables and default branch to
match the course. The default branch must already exist in the starter.

## Student projects

Manytask waits for the fork import and verifies its source and namespace.
Before granting the student Developer access, it applies and verifies:

- Private visibility and CI loaded from `.gitlab-ci.yml` in the instructor's
  starter project, rather than from the student's checkout.
- Pipeline-variable overrides disabled with `no_one_allowed`.
- Fork pipelines in the parent project, further forks, instance runners,
  and Auto DevOps disabled.
- A protected default branch allowing Developer pushes, Maintainer merges,
  and no force pushes. Other branch protection rules remain unchanged.

If default-branch protection needs replacement, an inherited rule is
rejected rather than removed. If the starter is private, Manytask also
grants the student Reporter access to that project. It does not add the
student to the course group. Course-group members are treated as course
administrators by the original 25.1 implementation.

Retries reuse the existing fork. Incomplete projects return to the course
join form, and access is not granted after a failed settings check.
Unexpected elevated student access must be corrected by a course operator;
existing course administrators retain their access.

## CI and Runner configuration

Configure a group Runner separately, including its tags, image access,
resource limits, and permissions needed by the course checker. For a
private starter, allow the students' subgroup in the starter's CI job-token
allowlist. Reporter membership and the allowlist serve different purposes.

If the grading image is private, configure image-pull credentials on the
Runner host. Do not grant students access to private grading materials
solely to let the Runner pull its image.

Configure the instructor's CI to grade the intended student's submissions.
For checkers that report `GITLAB_USER_ID`, teacher or automation pushes must
not be mistaken for student submissions.

Protected group variables are delivered to jobs on protected refs. Keeping
the CI in an instructor-controlled project and isolating student code in
the checker are separate requirements; branch protection alone does not
isolate a reporting token inside a job.

Before opening registration, test a new ordinary account through OAuth,
fork creation, a rejected submission, an accepted submission, and the
resulting spreadsheet row. Also test retrying a partially completed fork.
