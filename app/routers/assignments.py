"""교수의 과제 설정. 여섯 구역: 기본 정보와 역량, 사전 사후 평가, 과제 문제, 학습 자료, AI 응답 규칙, 도구 연동."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from ..ai import services
from ..ai.client import AIUnavailable
from ..ai.extract import extract_text
from ..deps import DB, OwnedAssignment, Professor
from ..models import AIRule, Assignment, Competency, CompetencyState, Material, TestAnswer, TestItem
from ..schemas import (
    AssignmentBrief,
    AssignmentCreate,
    AssignmentOut,
    AssignmentUpdate,
    CompetencyDraftIn,
    CompetencyDraftOut,
    CompetencyIn,
    CompetencyOut,
    ItemDraftIn,
    ItemDraftOut,
    ItemIn,
    ItemOut,
    MaterialOut,
    Phase,
    RuleIn,
    RuleOut,
)
from ..storage import remove_file, save_upload
from ..util import new_join_code, to_utc_naive
from .common import ai_error, assignment_brief, assignment_out

router = APIRouter(prefix="/api/assignments", tags=["assignments"])


class CompetencyUpsert(CompetencyIn):
    id: int | None = None


@router.get("", response_model=list[AssignmentBrief])
def list_assignments(user: Professor, db: DB) -> list[AssignmentBrief]:
    rows = (
        db.query(Assignment)
        .filter(Assignment.professor_id == user.id)
        .order_by(Assignment.created_at.desc())
        .all()
    )
    return [assignment_brief(a) for a in rows]


@router.post("", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def create_assignment(body: AssignmentCreate, user: Professor, db: DB) -> AssignmentOut:
    code = new_join_code()
    while db.query(Assignment).filter(Assignment.join_code == code).first():
        code = new_join_code()
    a = Assignment(
        professor_id=user.id,
        title=body.title.strip(),
        subject=body.subject.strip(),
        description=body.description,
        deadline=to_utc_naive(body.deadline),
        join_code=code,
        task_kind=body.task_kind,
        tool_slot=body.tool_slot,
        tool_name=body.tool_name,
        tool_config=body.tool_config,
        pre_time_limit_min=body.pre_time_limit_min,
        post_time_limit_min=body.post_time_limit_min,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.get("/{assignment_id}", response_model=AssignmentOut)
def get_assignment(a: OwnedAssignment) -> AssignmentOut:
    return assignment_out(a)


@router.patch("/{assignment_id}", response_model=AssignmentOut)
def update_assignment(body: AssignmentUpdate, a: OwnedAssignment, db: DB) -> AssignmentOut:
    data = body.model_dump(exclude_unset=True)
    clear = data.pop("clear_deadline", False)
    if "deadline" in data:
        data["deadline"] = to_utc_naive(data["deadline"])
    if clear:
        data["deadline"] = None
    if "status" in data and data["status"] == "open":
        problems = _publish_problems(a)
        if problems:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, " / ".join(problems))
    for key, value in data.items():
        setattr(a, key, value)
    db.commit()
    db.refresh(a)
    return assignment_out(a)


# ---------- 역량과 채점 기준 ----------
@router.put("/{assignment_id}/competencies", response_model=list[CompetencyOut])
def replace_competencies(body: list[CompetencyUpsert], a: OwnedAssignment, db: DB) -> list[CompetencyOut]:
    if not 1 <= len(body) <= 6:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "역량은 1개에서 6개 사이여야 한다")
    existing = {c.id: c for c in a.competencies}
    keep: set[int] = set()
    for order, item in enumerate(body):
        if item.id is not None and item.id in existing:
            c = existing[item.id]
            c.name, c.description = item.name.strip(), item.description
            c.pass_score, c.grading_criteria, c.order = item.pass_score, item.grading_criteria, order
            keep.add(c.id)
        else:
            c = Competency(
                assignment_id=a.id,
                order=order,
                name=item.name.strip(),
                description=item.description,
                pass_score=item.pass_score,
                grading_criteria=item.grading_criteria,
            )
            db.add(c)
    for cid, c in existing.items():
        if cid in keep:
            continue
        referenced = (
            db.query(TestItem).filter(TestItem.competency_id == cid).count()
            or db.query(CompetencyState).filter(CompetencyState.competency_id == cid).count()
        )
        if referenced:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"역량 {c.name}은 문항이나 학생 기록이 참조하고 있어 지울 수 없다"
            )
        db.delete(c)
    db.commit()
    db.refresh(a)
    return [CompetencyOut.model_validate(c) for c in a.competencies]


@router.post("/{assignment_id}/competencies/draft", response_model=CompetencyDraftOut)
def draft_competencies(body: CompetencyDraftIn, a: OwnedAssignment, db: DB) -> CompetencyDraftOut:
    try:
        result = services.draft_competencies(db, a, body.hint)
    except AIUnavailable as exc:
        raise ai_error(exc) from exc
    return CompetencyDraftOut(
        competencies=[
            CompetencyIn(name=c.name, description=c.description, pass_score=60, grading_criteria=c.grading_criteria)
            for c in result.competencies[:6]
        ]
    )


# ---------- 사전 사후 문항 ----------
@router.get("/{assignment_id}/items", response_model=list[ItemOut])
def list_items(a: OwnedAssignment, phase: Annotated[Phase, Query()]) -> list[ItemOut]:
    return [ItemOut.model_validate(i) for i in a.items if i.phase == phase]


@router.put("/{assignment_id}/items", response_model=list[ItemOut])
def replace_items(
    body: list[ItemIn], a: OwnedAssignment, db: DB, phase: Annotated[Phase, Query()]
) -> list[ItemOut]:
    if not a.competencies:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "역량을 먼저 입력해야 문항을 만들 수 있다")
    comp_ids = {c.id for c in a.competencies}
    for item in body:
        if item.competency_id not in comp_ids:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "문항이 가리키는 역량이 이 과제에 없다")
        if item.type == "choice" and (len(item.options) < 2 or not item.answer.strip()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "선택형은 보기 2개 이상과 정답이 필요하다")
        if item.type in ("short", "fill") and not item.answer.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "단답형과 완성형은 정답이 필요하다")
    old = [i for i in a.items if i.phase == phase]
    old_ids = [i.id for i in old]
    if old_ids and db.query(TestAnswer).filter(TestAnswer.item_id.in_(old_ids)).count():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 응시한 학생이 있어 이 단계의 문항을 바꿀 수 없다")
    for i in old:
        db.delete(i)
    db.flush()
    for order, item in enumerate(body):
        db.add(
            TestItem(
                assignment_id=a.id,
                competency_id=item.competency_id,
                phase=phase,
                order=order,
                type=item.type,
                stem=item.stem,
                options=item.options,
                answer=item.answer,
            )
        )
    db.commit()
    db.refresh(a)
    return [ItemOut.model_validate(i) for i in a.items if i.phase == phase]


@router.post("/{assignment_id}/items/draft", response_model=ItemDraftOut)
def draft_items(body: ItemDraftIn, a: OwnedAssignment, db: DB) -> ItemDraftOut:
    if not a.competencies:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "역량을 먼저 입력해야 문항 초안을 만들 수 있다")
    missing = [c.name for c in a.competencies if not c.grading_criteria.strip()]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "채점 기준이 비어 있는 역량이 있다: " + ", ".join(missing)
        )
    try:
        drafts = services.draft_items(db, a, body.phase)
    except AIUnavailable as exc:
        raise ai_error(exc) from exc
    by_name = {c.name.strip(): c.id for c in a.competencies}
    items: list[ItemIn] = []
    used: set[int] = set()
    for idx, d in enumerate(drafts):
        cid = by_name.get(d.competency_name.strip())
        if cid is None and idx < len(a.competencies):
            cid = a.competencies[idx].id
        if cid is None or cid in used:
            continue
        used.add(cid)
        items.append(ItemIn(competency_id=cid, type=d.type, stem=d.stem, options=d.options, answer=d.answer))
    return ItemDraftOut(items=items)


# ---------- 학습 자료와 과제 첨부 ----------
@router.post("/{assignment_id}/materials", response_model=MaterialOut, status_code=status.HTTP_201_CREATED)
async def upload_material(a: OwnedAssignment, db: DB, file: Annotated[UploadFile, File()]) -> MaterialOut:
    from ..config import settings

    path, original, size = await save_upload(file, f"assignments/{a.id}/materials", settings.max_material_mb)
    text = extract_text(path)
    m = Material(
        assignment_id=a.id,
        filename=original,
        path=str(path),
        mime=file.content_type or "",
        size=size,
        extracted_text=text,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return MaterialOut(
        id=m.id, filename=m.filename, mime=m.mime, size=m.size, has_text=bool(m.extracted_text), created_at=m.created_at
    )


@router.delete("/{assignment_id}/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_material(material_id: int, a: OwnedAssignment, db: DB) -> None:
    m = db.get(Material, material_id)
    if m is None or m.assignment_id != a.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "자료를 찾을 수 없다")
    remove_file(m.path)
    db.delete(m)
    db.commit()


@router.get("/{assignment_id}/materials/{material_id}/file")
def download_material(material_id: int, a: OwnedAssignment, db: DB) -> FileResponse:
    m = db.get(Material, material_id)
    if m is None or m.assignment_id != a.id or not Path(m.path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "자료를 찾을 수 없다")
    return FileResponse(m.path, filename=m.filename)


@router.post("/{assignment_id}/attachment", response_model=AssignmentOut)
async def upload_attachment(a: OwnedAssignment, db: DB, file: Annotated[UploadFile, File()]) -> AssignmentOut:
    from ..config import settings

    path, original, _ = await save_upload(file, f"assignments/{a.id}/attachment", settings.max_material_mb)
    remove_file(a.attachment_path)
    a.attachment_name, a.attachment_path = original, str(path)
    db.commit()
    db.refresh(a)
    return assignment_out(a)


@router.get("/{assignment_id}/attachment")
def download_attachment(a: OwnedAssignment) -> FileResponse:
    if not a.attachment_path or not Path(a.attachment_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "첨부 파일이 없다")
    return FileResponse(a.attachment_path, filename=a.attachment_name)


# ---------- AI 응답 규칙 ----------
@router.put("/{assignment_id}/rules", response_model=list[RuleOut])
def replace_rules(body: list[RuleIn], a: OwnedAssignment, db: DB) -> list[RuleOut]:
    for r in list(a.rules):
        db.delete(r)
    db.flush()
    for r in body:
        db.add(AIRule(assignment_id=a.id, target=r.target, text=r.text.strip()))
    db.commit()
    db.refresh(a)
    return [RuleOut.model_validate(r) for r in a.rules]


# ---------- 공개 ----------
def _publish_problems(a: Assignment) -> list[str]:
    problems: list[str] = []
    if not a.competencies:
        problems.append("역량이 없다")
    for c in a.competencies:
        if not c.grading_criteria.strip():
            problems.append(f"역량 {c.name}의 채점 기준이 비어 있다")
    for phase, label in (("pre", "사전평가"), ("post", "사후평가")):
        covered = {i.competency_id for i in a.items if i.phase == phase}
        missing = [c.name for c in a.competencies if c.id not in covered]
        if missing:
            problems.append(f"{label} 문항이 없는 역량: " + ", ".join(missing))
    if a.deadline is None:
        problems.append("과제 마감이 없다")
    if a.tool_slot == 3 and not (a.tool_config or {}).get("program"):
        problems.append("자리 3 과제는 프로그램 이름이 필요하다")
    return problems


@router.post("/{assignment_id}/publish", response_model=AssignmentOut)
def publish(a: OwnedAssignment, db: DB) -> AssignmentOut:
    problems = _publish_problems(a)
    if problems:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, " / ".join(problems))
    a.status = "open"
    db.commit()
    db.refresh(a)
    return assignment_out(a)
