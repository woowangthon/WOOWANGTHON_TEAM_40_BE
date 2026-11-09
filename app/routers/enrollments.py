"""학생의 참여와 현재 상태."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ..deps import DB, MyEnrollment, Student
from ..models import Assignment, CompetencyState, Enrollment, Material
from ..schemas import EnrollmentBrief, EnrollmentOut, JoinIn
from .common import assignment_student_view, attempt_out, deadline_passed, state_out

router = APIRouter(prefix="/api/enrollments", tags=["enrollments"])


def enrollment_out(e: Enrollment) -> EnrollmentOut:
    a = e.assignment
    return EnrollmentOut(
        id=e.id,
        assignment_id=a.id,
        stage=e.stage,
        joined_at=e.joined_at,
        deadline_passed=deadline_passed(a),
        assignment=assignment_student_view(a, _professor_name(a)),
        states=[state_out(s) for s in sorted(e.competency_states, key=lambda s: s.competency.order)],
        attempts=[attempt_out(t) for t in e.task_attempts],
    )


def _professor_name(a: Assignment) -> str:
    return a.professor.name if a.professor else ""


@router.post("/join", response_model=EnrollmentOut, status_code=status.HTTP_201_CREATED)
def join(body: JoinIn, user: Student, db: DB) -> EnrollmentOut:
    code = body.code.strip().upper()
    a = db.query(Assignment).filter(Assignment.join_code == code).first()
    if a is None or a.status != "open":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "참여 코드에 해당하는 공개된 과제가 없다")
    existing = (
        db.query(Enrollment)
        .filter(Enrollment.assignment_id == a.id, Enrollment.student_id == user.id)
        .first()
    )
    if existing:
        return enrollment_out(existing)
    e = Enrollment(assignment_id=a.id, student_id=user.id, stage="pre")
    db.add(e)
    db.flush()
    for c in a.competencies:
        db.add(CompetencyState(enrollment_id=e.id, competency_id=c.id))
    db.commit()
    db.refresh(e)
    return enrollment_out(e)


@router.get("", response_model=list[EnrollmentBrief])
def list_enrollments(user: Student, db: DB) -> list[EnrollmentBrief]:
    rows = db.query(Enrollment).filter(Enrollment.student_id == user.id).order_by(Enrollment.joined_at.desc()).all()
    out: list[EnrollmentBrief] = []
    for e in rows:
        a = e.assignment
        out.append(
            EnrollmentBrief(
                id=e.id,
                assignment_id=a.id,
                stage=e.stage,
                joined_at=e.joined_at,
                deadline_passed=deadline_passed(a),
                title=a.title,
                subject=a.subject,
                professor_name=_professor_name(a),
                deadline=a.deadline,
                tool_slot=a.tool_slot,
                tool_name=a.tool_name,
            )
        )
    return out


@router.get("/{enrollment_id}", response_model=EnrollmentOut)
def get_enrollment(e: MyEnrollment) -> EnrollmentOut:
    return enrollment_out(e)


@router.get("/{enrollment_id}/attachment")
def attachment(e: MyEnrollment) -> FileResponse:
    a = e.assignment
    if not a.attachment_path or not Path(a.attachment_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "첨부 파일이 없다")
    return FileResponse(a.attachment_path, filename=a.attachment_name)


@router.get("/{enrollment_id}/materials/{material_id}/file")
def material_file(material_id: int, e: MyEnrollment, db: DB) -> FileResponse:
    m = db.get(Material, material_id)
    if m is None or m.assignment_id != e.assignment_id or not Path(m.path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "자료를 찾을 수 없다")
    return FileResponse(m.path, filename=m.filename)
