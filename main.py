"""
===============================================================================
 노션 워크파일 지연 업무 대시보드 (Flask 단일 파일 버전)
===============================================================================

■ 기능
  1) 메인 대시보드 : 노션 DB의 전체 업무 + 오늘 기준 '지연 업무'를 테이블로 표시
  2) 지연 건 요약   : 마감일이 지났고 '완료'가 아닌 항목을 자동 필터링해 상단 카드로 요약
  3) 디스코드 알림  : [디스코드 알림 전송] 버튼 → 담당자별로 묶어 웹훅 전송 (미리보기 지원)
  4) 지연 사유/변경 일정 입력 : 화면에서 바로 입력 → [저장] 누르면 노션 페이지에 반영

■ 폴더 구조 (파일 3개면 끝)
  workfile-dashboard/
   ├─ app.py            ← 이 파일
   ├─ requirements.txt
   └─ .env              ← .env.example 을 복사해서 값 채우기

■ 설치 (Python 3.9 이상)
  # 1. 가상환경 만들기 (권장)
  python -m venv venv
  # Windows       : venv\\Scripts\\activate
  # macOS / Linux : source venv/bin/activate

  # 2. 라이브러리 설치
  pip install -r requirements.txt
  #   (또는) pip install flask notion-client requests python-dotenv tzdata

■ 노션 준비
  1) https://www.notion.so/profile/integrations → [새 API 통합] 생성 → "내부 통합 시크릿" 복사
     → .env 의 NOTION_TOKEN 에 붙여넣기
  2) 워크파일 데이터베이스 페이지 오른쪽 위 [⋯] → [연결] → 방금 만든 통합 추가
     (이걸 안 하면 404 object_not_found 오류가 납니다)
  3) 데이터베이스 URL 에서 ID 복사
     https://www.notion.so/내워크스페이스/1a2b3c4d5e6f...?v=...  ← ?v= 앞 32자리
     → .env 의 NOTION_DATABASE_ID (URL 통째로 넣어도 자동으로 ID만 추출합니다)
  4) DB 속성 이름이 아래 기본값과 다르면 .env 의 PROP_* 값을 실제 이름으로 바꾸세요.
       업무명(제목) / 담당자(사람·선택·텍스트) / 마감일(날짜) / 상태(상태·선택·체크박스)
       지연 사유(텍스트) / 변경 일정(날짜)   ← 이 두 개는 입력 기능을 쓰려면 DB에 새로 추가

■ 디스코드 준비
  채널 설정(⚙) → [연동] → [웹후크] → [새 웹후크] → [웹후크 URL 복사]
  → .env 의 DISCORD_WEBHOOK_URL 에 붙여넣기
  (선택) 담당자를 실제로 @멘션하려면 DISCORD_MENTIONS 에 {"노션이름": "디스코드유저ID"} 입력
         디스코드 [설정 → 고급 → 개발자 모드] 켠 뒤 사용자 우클릭 → [ID 복사]

■ 실행
  python app.py
  → 브라우저에서 http://127.0.0.1:5000 접속
  (사내 다른 PC에서도 접속하려면 .env 에 HOST=0.0.0.0 설정 후 http://내PC IP:5000)

■ 지연 판정 규칙
  - 기준일 = 오늘 (TIMEZONE, 기본 Asia/Seoul)
  - 상태가 DONE_STATUSES(기본 "완료,Done") 에 해당하면 지연 아님
  - USE_NEW_DUE_FOR_DELAY=true(기본) 이면 '변경 일정'이 있는 업무는 변경 일정으로 지연 여부 판단
    → 일정을 재조정한 업무는 새 날짜가 지나기 전까지 지연 목록에서 빠집니다.
  - 마감일(기준 날짜)이 오늘보다 이전이면 지연 (오늘 마감은 지연 아님)
===============================================================================
"""

import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request
from notion_client import APIResponseError, Client

# -----------------------------------------------------------------------------
# 1. 환경 변수 로드
# -----------------------------------------------------------------------------
load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID_RAW = os.getenv("NOTION_DATABASE_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()  # 디스코드 메시지 하단에 붙일 대시보드 주소(선택)

TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")

# 노션 DB 속성 이름 (실제 DB의 컬럼명과 똑같이 맞춰야 함)
PROP_TITLE = os.getenv("PROP_TITLE", "업무명")
PROP_ASSIGNEE = os.getenv("PROP_ASSIGNEE", "담당자")
PROP_DUE = os.getenv("PROP_DUE", "마감일")
PROP_STATUS = os.getenv("PROP_STATUS", "상태")
PROP_REASON = os.getenv("PROP_REASON", "지연 사유")
PROP_NEW_DUE = os.getenv("PROP_NEW_DUE", "변경 일정")

DONE_STATUSES = {s.strip() for s in os.getenv("DONE_STATUSES", "완료,Done").split(",") if s.strip()}
USE_NEW_DUE_FOR_DELAY = os.getenv("USE_NEW_DUE_FOR_DELAY", "true").lower() in ("1", "true", "yes", "y")

try:
    DISCORD_MENTIONS = json.loads(os.getenv("DISCORD_MENTIONS", "{}") or "{}")
except json.JSONDecodeError:
    DISCORD_MENTIONS = {}

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
DEBUG = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")

UNASSIGNED = "미지정"
DISCORD_LIMIT = 1900  # 디스코드 메시지 최대 2000자 → 여유 있게 자름

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("workfile")


def normalize_notion_id(raw: str) -> str:
    """URL 이나 하이픈 섞인 ID 에서 32자리 노션 ID 만 뽑아낸다."""
    if not raw:
        return ""
    last = raw.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]  # URL 마지막 조각
    compact = last.replace("-", "")
    found = re.search(r"[0-9a-fA-F]{32}$", compact)  # 페이지 제목 뒤에 붙은 마지막 32자리
    return found.group(0) if found else raw


NOTION_DATABASE_ID = normalize_notion_id(NOTION_DATABASE_ID_RAW)

# notion_version 을 고정해 두면 notion-client 버전이 바뀌어도 동일하게 동작한다.
notion = Client(auth=NOTION_TOKEN, notion_version="2022-06-28") if NOTION_TOKEN else None

app = Flask(__name__)


# -----------------------------------------------------------------------------
# 2. 노션 데이터 조회 / 파싱
# -----------------------------------------------------------------------------
def today_local() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def config_errors() -> list:
    errors = []
    if not NOTION_TOKEN:
        errors.append("NOTION_TOKEN 이 비어 있습니다 (.env 확인).")
    if not NOTION_DATABASE_ID:
        errors.append("NOTION_DATABASE_ID 가 비어 있습니다 (.env 확인).")
    return errors


def get_db_schema() -> dict:
    """DB 속성 목록 {속성이름: 타입} 반환."""
    db = notion.request(path=f"databases/{NOTION_DATABASE_ID}", method="GET")
    return {name: p.get("type") for name, p in db.get("properties", {}).items()}


def query_all_pages() -> list:
    """DB 의 모든 페이지를 페이지네이션하며 가져온다 (100개 단위)."""
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = notion.request(path=f"databases/{NOTION_DATABASE_ID}/query", method="POST", body=body)
        pages.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return [p for p in pages if not p.get("archived") and not p.get("in_trash")]


def _person_name(p: dict) -> str:
    return (p or {}).get("name") or "(이름없음)"


def prop_to_text(prop: dict) -> str:
    """어떤 타입의 노션 속성이든 사람이 읽을 수 있는 문자열로 변환."""
    if not prop:
        return ""
    t = prop.get("type")
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in (v or []))
    if t in ("select", "status"):
        return (v or {}).get("name", "") if v else ""
    if t == "multi_select":
        return ", ".join(x.get("name", "") for x in (v or []))
    if t == "people":
        return ", ".join(_person_name(p) for p in (v or []))
    if t in ("created_by", "last_edited_by"):
        return _person_name(v)
    if t == "date":
        return (v or {}).get("start", "") if v else ""
    if t == "checkbox":
        return "완료" if v else "미완료"
    if t in ("number", "url", "email", "phone_number", "created_time", "last_edited_time"):
        return "" if v is None else str(v)
    if t == "formula":
        ft = (v or {}).get("type")
        fv = (v or {}).get(ft)
        if ft == "date":
            return (fv or {}).get("start", "") if fv else ""
        return "" if fv is None else str(fv)
    if t == "rollup":
        rt = (v or {}).get("type")
        if rt == "array":
            return ", ".join(prop_to_text(x) for x in v.get("array", []))
        rv = (v or {}).get(rt)
        if rt == "date":
            return (rv or {}).get("start", "") if rv else ""
        return "" if rv is None else str(rv)
    return ""


def prop_to_list(prop: dict) -> list:
    """담당자처럼 여러 명일 수 있는 속성을 리스트로 변환."""
    if not prop:
        return []
    t = prop.get("type")
    v = prop.get(t)
    if t == "people":
        return [_person_name(p) for p in (v or [])]
    if t == "multi_select":
        return [x.get("name", "") for x in (v or []) if x.get("name")]
    text = prop_to_text(prop)
    return [s.strip() for s in re.split(r"[,/]", text) if s.strip()]


def parse_date(s: str):
    """'2026-09-30' 또는 '2026-09-30T10:00:00.000+09:00' → date (현지 시간대 기준)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(ZoneInfo(TIMEZONE))
        return dt.date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def parse_page(page: dict, today: date) -> dict:
    props = page.get("properties", {})

    # 제목 속성은 이름이 달라도 자동 탐지
    title_prop = props.get(PROP_TITLE)
    if not title_prop or title_prop.get("type") != "title":
        title_prop = next((p for p in props.values() if p.get("type") == "title"), None)

    status_prop = props.get(PROP_STATUS)
    status = prop_to_text(status_prop)
    if status_prop and status_prop.get("type") == "checkbox":
        is_done = bool(status_prop.get("checkbox"))
    else:
        is_done = status in DONE_STATUSES

    due_str = prop_to_text(props.get(PROP_DUE))
    new_due_str = prop_to_text(props.get(PROP_NEW_DUE))
    due = parse_date(due_str)
    new_due = parse_date(new_due_str)

    effective_due = (new_due or due) if USE_NEW_DUE_FOR_DELAY else due
    is_delayed = bool(effective_due and not is_done and effective_due < today)
    overdue_days = (today - effective_due).days if is_delayed else 0

    assignees = prop_to_list(props.get(PROP_ASSIGNEE)) or [UNASSIGNED]

    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "title": prop_to_text(title_prop) or "(제목 없음)",
        "assignees": assignees,
        "assignee_text": ", ".join(assignees),
        "status": status or "-",
        "is_done": is_done,
        "due": due.isoformat() if due else "",
        "new_due": new_due.isoformat() if new_due else "",
        "effective_due": effective_due.isoformat() if effective_due else "",
        "reason": prop_to_text(props.get(PROP_REASON)),
        "is_delayed": is_delayed,
        "overdue_days": overdue_days,
    }


def load_tasks():
    """(전체 업무, 지연 업무, 오늘 날짜) 반환."""
    today = today_local()
    tasks = [parse_page(p, today) for p in query_all_pages()]
    tasks.sort(key=lambda t: (t["effective_due"] or "9999-99-99", t["title"]))
    delayed = sorted([t for t in tasks if t["is_delayed"]], key=lambda t: -t["overdue_days"])
    return tasks, delayed, today


def group_by_assignee(delayed: list) -> dict:
    """담당자별로 묶기 (한 업무에 담당자가 여럿이면 각자에게 포함)."""
    grouped = defaultdict(list)
    for t in delayed:
        for a in t["assignees"]:
            grouped[a].append(t)
    # 지연 건수 많은 담당자 먼저, 미지정은 맨 뒤
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == UNASSIGNED, -len(kv[1]), kv[0])))


# -----------------------------------------------------------------------------
# 3. 디스코드 알림
# -----------------------------------------------------------------------------
def mention_of(name: str) -> str:
    uid = DISCORD_MENTIONS.get(name)
    return f"<@{uid}>" if uid else f"**{name}**"


def build_discord_messages(delayed: list, today: date) -> list:
    """담당자별로 정리된 디스코드 메시지 목록(각 2000자 이하)을 만든다."""
    if not delayed:
        return [f"✅ **[워크파일] {today.isoformat()} 기준 지연 업무가 없습니다.**"]

    grouped = group_by_assignee(delayed)
    header = (
        f"📢 **[워크파일] 지연 업무 알림** ({today.isoformat()} 기준)\n"
        f"총 **{len(delayed)}건** · 담당자 **{len(grouped)}명**\n"
    )
    blocks = []
    for name, items in grouped.items():
        lines = [f"\n👤 {mention_of(name)} — {len(items)}건"]
        for t in items:
            line = f"• **{t['title']}** | 마감 {t['due'] or '-'}"
            if t["new_due"]:
                line += f" → 변경 {t['new_due']}"
            line += f" | 상태 {t['status']} | ⏰ D+{t['overdue_days']}"
            if t["reason"]:
                line += f"\n   └ 사유: {t['reason']}"
            lines.append(line)
        blocks.append("\n".join(lines))

    footer = f"\n\n🔗 대시보드: {DASHBOARD_URL}" if DASHBOARD_URL else ""

    # 2000자 제한에 맞춰 여러 메시지로 분할
    messages, current = [], header
    for block in blocks:
        if len(block) > DISCORD_LIMIT:  # 한 사람 분량이 너무 길면 줄 단위로 자름
            for line in block.split("\n"):
                if len(current) + len(line) + 1 > DISCORD_LIMIT:
                    messages.append(current)
                    current = ""
                current += "\n" + line
            continue
        if len(current) + len(block) > DISCORD_LIMIT:
            messages.append(current)
            current = ""
        current += block
    if footer and len(current) + len(footer) > DISCORD_LIMIT:
        messages.append(current)
        current = ""
    current += footer
    if current.strip():
        messages.append(current)
    return messages


def post_to_discord(content: str, retries: int = 3) -> None:
    payload = {
        "content": content,
        "username": "워크파일 알리미",
        "allowed_mentions": {"parse": ["users"]},  # <@ID> 멘션만 허용 (@everyone 차단)
    }
    for _ in range(retries):
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 429:  # 속도 제한 → 안내된 시간만큼 대기 후 재시도
            wait = float(r.json().get("retry_after", 1))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return
    raise RuntimeError("디스코드 속도 제한으로 전송에 실패했습니다.")


# -----------------------------------------------------------------------------
# 4. 라우트
# -----------------------------------------------------------------------------
def notion_error_message(e: Exception) -> str:
    if isinstance(e, APIResponseError):
        code = getattr(e, "code", "")
        if code == "object_not_found":
            return "노션 DB를 찾을 수 없습니다. DB ID가 맞는지, DB에 통합(Integration) 연결을 추가했는지 확인하세요."
        if code == "unauthorized":
            return "노션 토큰이 올바르지 않습니다. NOTION_TOKEN 을 확인하세요."
        return f"노션 API 오류 ({code}): {e}"
    return f"오류: {e}"


@app.route("/")
def dashboard():
    errors = config_errors()
    tasks, delayed, today = [], [], today_local()
    schema = {}
    if not errors:
        try:
            tasks, delayed, today = load_tasks()
            schema = get_db_schema()
        except Exception as e:  # noqa: BLE001
            log.exception("노션 조회 실패")
            errors.append(notion_error_message(e))

    warnings = []
    if schema:
        for label, name in [("담당자", PROP_ASSIGNEE), ("마감일", PROP_DUE), ("상태", PROP_STATUS)]:
            if name not in schema:
                warnings.append(f"DB에 '{name}' 속성이 없습니다 ({label}). .env 의 PROP_* 이름을 확인하세요.")
        if PROP_REASON not in schema or PROP_NEW_DUE not in schema:
            warnings.append(
                f"지연 사유/변경 일정 입력을 쓰려면 DB에 '{PROP_REASON}'(텍스트), '{PROP_NEW_DUE}'(날짜) 속성을 추가하세요."
            )
    can_edit = bool(schema) and PROP_REASON in schema and PROP_NEW_DUE in schema

    grouped = group_by_assignee(delayed)
    summary = {
        "total": len(tasks),
        "done": sum(1 for t in tasks if t["is_done"]),
        "delayed": len(delayed),
        "assignees": len(grouped),
        "max_overdue": max((t["overdue_days"] for t in delayed), default=0),
    }
    return render_template_string(
        TEMPLATE,
        tasks=tasks,
        delayed=delayed,
        grouped=grouped,
        summary=summary,
        today=today.isoformat(),
        errors=errors,
        warnings=warnings,
        can_edit=can_edit,
        webhook_ready=bool(DISCORD_WEBHOOK_URL),
        prop_reason=PROP_REASON,
        prop_new_due=PROP_NEW_DUE,
    )


@app.get("/api/tasks")
def api_tasks():
    """JSON 으로 전체/지연 업무 조회 (다른 도구 연동용)."""
    if config_errors():
        return jsonify(ok=False, error=config_errors()), 400
    try:
        tasks, delayed, today = load_tasks()
        return jsonify(ok=True, today=today.isoformat(), tasks=tasks, delayed=delayed)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=notion_error_message(e)), 500


@app.get("/api/notify/preview")
def api_notify_preview():
    """디스코드로 보낼 메시지를 미리 보기."""
    if config_errors():
        return jsonify(ok=False, error=" ".join(config_errors())), 400
    try:
        _, delayed, today = load_tasks()
        return jsonify(ok=True, messages=build_discord_messages(delayed, today))
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=notion_error_message(e)), 500


@app.post("/api/notify")
def api_notify():
    """지연 업무를 담당자별로 정리해 디스코드 웹훅으로 전송."""
    if not DISCORD_WEBHOOK_URL:
        return jsonify(ok=False, error="DISCORD_WEBHOOK_URL 이 설정되지 않았습니다 (.env 확인)."), 400
    if config_errors():
        return jsonify(ok=False, error=" ".join(config_errors())), 400
    try:
        _, delayed, today = load_tasks()  # 전송 직전 최신 데이터로 다시 조회
        messages = build_discord_messages(delayed, today)
        for i, msg in enumerate(messages):
            post_to_discord(msg)
            if i < len(messages) - 1:
                time.sleep(0.6)  # 연속 전송 시 속도 제한 방지
        log.info("디스코드 전송 완료: 지연 %d건, 메시지 %d개", len(delayed), len(messages))
        return jsonify(ok=True, delayed=len(delayed), messages=len(messages))
    except requests.HTTPError as e:
        return jsonify(ok=False, error=f"디스코드 전송 실패: {e.response.status_code} {e.response.text[:200]}"), 502
    except Exception as e:  # noqa: BLE001
        log.exception("알림 전송 실패")
        return jsonify(ok=False, error=notion_error_message(e)), 500


@app.post("/api/tasks/<page_id>")
def api_update_task(page_id):
    """지연 사유 / 변경 일정을 노션 페이지에 저장."""
    data = request.get_json(silent=True) or {}
    try:
        schema = get_db_schema()
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=notion_error_message(e)), 500

    props = {}
    if "reason" in data:
        if PROP_REASON not in schema:
            return jsonify(ok=False, error=f"DB에 '{PROP_REASON}' 속성이 없습니다."), 400
        reason = (data.get("reason") or "").strip()[:2000]
        rich = [{"type": "text", "text": {"content": reason}}] if reason else []
        ptype = schema[PROP_REASON]
        if ptype == "rich_text":
            props[PROP_REASON] = {"rich_text": rich}
        elif ptype == "select":
            props[PROP_REASON] = {"select": {"name": reason[:100]} if reason else None}
        else:
            return jsonify(ok=False, error=f"'{PROP_REASON}' 속성은 텍스트 타입이어야 합니다 (현재: {ptype})."), 400

    if "new_due" in data:
        if PROP_NEW_DUE not in schema:
            return jsonify(ok=False, error=f"DB에 '{PROP_NEW_DUE}' 속성이 없습니다."), 400
        if schema[PROP_NEW_DUE] != "date":
            return jsonify(ok=False, error=f"'{PROP_NEW_DUE}' 속성은 날짜 타입이어야 합니다."), 400
        new_due = (data.get("new_due") or "").strip()
        if new_due:
            try:
                date.fromisoformat(new_due)
            except ValueError:
                return jsonify(ok=False, error="날짜 형식은 YYYY-MM-DD 여야 합니다."), 400
        props[PROP_NEW_DUE] = {"date": {"start": new_due} if new_due else None}

    if not props:
        return jsonify(ok=False, error="변경할 값이 없습니다."), 400

    try:
        notion.pages.update(page_id=page_id, properties=props)
        return jsonify(ok=True)
    except Exception as e:  # noqa: BLE001
        log.exception("노션 업데이트 실패")
        return jsonify(ok=False, error=notion_error_message(e)), 500


# -----------------------------------------------------------------------------
# 5. HTML 템플릿 (Bootstrap 5)
# -----------------------------------------------------------------------------
TEMPLATE = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>워크파일 일정 대시보드</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css" rel="stylesheet">
  <style>
    body { font-family: Pretendard, -apple-system, sans-serif; background: #f5f6f8; }
    .stat-card { border: 0; border-radius: 14px; }
    .stat-card .num { font-size: 2rem; font-weight: 700; line-height: 1.1; }
    .stat-card .label { color: #6c757d; font-size: .85rem; }
    .table td, .table th { vertical-align: middle; font-size: .9rem; }
    .table thead th { background: #f1f3f5; white-space: nowrap; }
    .reason-input { min-width: 200px; }
    .assignee-chip { background: #fff; border: 1px solid #dee2e6; border-radius: 999px; padding: .35rem .8rem; font-size: .85rem; }
    .row-delayed { background: #fff5f5 !important; }
    pre.preview { white-space: pre-wrap; background: #2b2d31; color: #dbdee1; padding: 1rem; border-radius: 8px; font-size: .85rem; }
    a.task-link { text-decoration: none; color: inherit; }
    a.task-link:hover { text-decoration: underline; }
  </style>
</head>
<body>
<nav class="navbar bg-dark navbar-dark mb-4">
  <div class="container-fluid px-4">
    <span class="navbar-brand fw-semibold"><i class="bi bi-kanban"></i> 워크파일 일정 대시보드</span>
    <div class="d-flex gap-2 align-items-center">
      <span class="text-white-50 small me-2">기준일 {{ today }}</span>
      <a href="/" class="btn btn-outline-light btn-sm"><i class="bi bi-arrow-clockwise"></i> 새로고침</a>
      <button class="btn btn-outline-light btn-sm" onclick="previewNotify()" {% if errors %}disabled{% endif %}>
        <i class="bi bi-eye"></i> 알림 미리보기</button>
      <button id="btnNotify" class="btn btn-primary btn-sm" onclick="sendNotify()"
              {% if errors or not webhook_ready %}disabled{% endif %}>
        <i class="bi bi-discord"></i> 디스코드 알림 전송</button>
    </div>
  </div>
</nav>

<div class="container-fluid px-4 pb-5">

  {% for e in errors %}
    <div class="alert alert-danger"><i class="bi bi-exclamation-octagon"></i> {{ e }}</div>
  {% endfor %}
  {% for w in warnings %}
    <div class="alert alert-warning py-2 small"><i class="bi bi-info-circle"></i> {{ w }}</div>
  {% endfor %}
  {% if not webhook_ready %}
    <div class="alert alert-secondary py-2 small">DISCORD_WEBHOOK_URL 이 설정되지 않아 알림 전송 버튼이 비활성화되었습니다.</div>
  {% endif %}

  <!-- ===== 요약 카드 ===== -->
  <div class="row g-3 mb-4">
    <div class="col-6 col-lg"><div class="card stat-card shadow-sm p-3">
      <div class="label">전체 업무</div><div class="num">{{ summary.total }}</div></div></div>
    <div class="col-6 col-lg"><div class="card stat-card shadow-sm p-3">
      <div class="label">완료</div><div class="num text-success">{{ summary.done }}</div></div></div>
    <div class="col-6 col-lg"><div class="card stat-card shadow-sm p-3 border-start border-4 border-danger">
      <div class="label">일정 지연</div><div class="num text-danger">{{ summary.delayed }}</div></div></div>
    <div class="col-6 col-lg"><div class="card stat-card shadow-sm p-3">
      <div class="label">지연 담당자</div><div class="num">{{ summary.assignees }}명</div></div></div>
    <div class="col-12 col-lg"><div class="card stat-card shadow-sm p-3">
      <div class="label">최장 지연</div><div class="num text-warning">D+{{ summary.max_overdue }}</div></div></div>
  </div>

  <!-- ===== 담당자별 지연 요약 ===== -->
  {% if grouped %}
  <div class="card shadow-sm mb-4 border-0">
    <div class="card-body">
      <h6 class="fw-semibold mb-3"><i class="bi bi-people"></i> 담당자별 지연 현황</h6>
      <div class="d-flex flex-wrap gap-2">
        {% for name, items in grouped.items() %}
          <span class="assignee-chip">{{ name }} <span class="badge bg-danger rounded-pill ms-1">{{ items|length }}</span></span>
        {% endfor %}
      </div>
    </div>
  </div>
  {% endif %}

  <!-- ===== 탭 ===== -->
  <ul class="nav nav-tabs" role="tablist">
    <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tabDelayed">
      <i class="bi bi-alarm"></i> 지연 업무 <span class="badge bg-danger">{{ delayed|length }}</span></button></li>
    <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tabAll">
      <i class="bi bi-list-task"></i> 전체 업무 <span class="badge bg-secondary">{{ tasks|length }}</span></button></li>
  </ul>

  <div class="tab-content bg-white shadow-sm rounded-bottom p-3">
    <!-- 지연 업무 -->
    <div class="tab-pane fade show active" id="tabDelayed">
      {% if delayed %}
      <div class="table-responsive">
        <table class="table table-hover mb-0">
          <thead><tr>
            <th>업무명</th><th>담당자</th><th>상태</th><th>마감일</th><th>지연</th>
            <th>{{ prop_reason }}</th><th>{{ prop_new_due }}</th><th></th>
          </tr></thead>
          <tbody>
          {% for t in delayed %}
            <tr data-id="{{ t.id }}">
              <td><a class="task-link fw-semibold" href="{{ t.url }}" target="_blank">{{ t.title }} <i class="bi bi-box-arrow-up-right small text-muted"></i></a></td>
              <td>{{ t.assignee_text }}</td>
              <td><span class="badge text-bg-light border">{{ t.status }}</span></td>
              <td class="text-nowrap">{{ t.due or '-' }}</td>
              <td><span class="badge bg-danger">D+{{ t.overdue_days }}</span></td>
              <td><input class="form-control form-control-sm reason-input" value="{{ t.reason }}"
                         placeholder="지연 사유 입력" {% if not can_edit %}disabled{% endif %}></td>
              <td><input type="date" class="form-control form-control-sm newdue-input" value="{{ t.new_due }}"
                         {% if not can_edit %}disabled{% endif %}></td>
              <td><button class="btn btn-sm btn-outline-primary text-nowrap" onclick="saveRow(this)"
                          {% if not can_edit %}disabled{% endif %}><i class="bi bi-save"></i> 저장</button></td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% elif not errors %}
        <div class="text-center text-muted py-5"><i class="bi bi-emoji-smile fs-2"></i><div class="mt-2">지연된 업무가 없습니다 🎉</div></div>
      {% endif %}
    </div>

    <!-- 전체 업무 -->
    <div class="tab-pane fade" id="tabAll">
      <div class="d-flex flex-wrap gap-2 mb-3">
        <input id="searchBox" class="form-control form-control-sm" style="max-width:280px"
               placeholder="업무명·담당자 검색" oninput="filterAll()">
        <select id="statusFilter" class="form-select form-select-sm" style="max-width:160px" onchange="filterAll()">
          <option value="all">전체 상태</option>
          <option value="delayed">지연만</option>
          <option value="open">미완료만</option>
          <option value="done">완료만</option>
        </select>
      </div>
      <div class="table-responsive">
        <table class="table table-hover mb-0" id="allTable">
          <thead><tr>
            <th>업무명</th><th>담당자</th><th>상태</th><th>마감일</th><th>변경 일정</th><th>지연 사유</th><th>구분</th>
          </tr></thead>
          <tbody>
          {% for t in tasks %}
            <tr class="{{ 'row-delayed' if t.is_delayed }}"
                data-kind="{{ 'delayed' if t.is_delayed else ('done' if t.is_done else 'open') }}"
                data-search="{{ (t.title ~ ' ' ~ t.assignee_text)|lower }}">
              <td><a class="task-link" href="{{ t.url }}" target="_blank">{{ t.title }}</a></td>
              <td>{{ t.assignee_text }}</td>
              <td><span class="badge text-bg-light border">{{ t.status }}</span></td>
              <td class="text-nowrap">{{ t.due or '-' }}</td>
              <td class="text-nowrap">{{ t.new_due or '-' }}</td>
              <td class="small text-muted">{{ t.reason }}</td>
              <td>
                {% if t.is_done %}<span class="badge bg-success">완료</span>
                {% elif t.is_delayed %}<span class="badge bg-danger">지연 D+{{ t.overdue_days }}</span>
                {% else %}<span class="badge bg-primary-subtle text-primary">진행</span>{% endif %}
              </td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- 미리보기 모달 -->
<div class="modal fade" id="previewModal" tabindex="-1">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header"><h6 class="modal-title"><i class="bi bi-discord"></i> 디스코드 전송 미리보기</h6>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>
      <div class="modal-body" id="previewBody">불러오는 중…</div>
      <div class="modal-footer">
        <button class="btn btn-secondary btn-sm" data-bs-dismiss="modal">닫기</button>
        <button class="btn btn-primary btn-sm" onclick="sendNotify(true)" {% if not webhook_ready %}disabled{% endif %}>
          <i class="bi bi-send"></i> 이대로 전송</button>
      </div>
    </div>
  </div>
</div>

<!-- 토스트 -->
<div class="toast-container position-fixed bottom-0 end-0 p-3">
  <div id="toast" class="toast align-items-center border-0" role="alert">
    <div class="d-flex"><div class="toast-body" id="toastBody"></div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
function toast(msg, ok = true) {
  const el = document.getElementById('toast');
  el.className = 'toast align-items-center border-0 text-white ' + (ok ? 'bg-success' : 'bg-danger');
  document.getElementById('toastBody').textContent = msg;
  bootstrap.Toast.getOrCreateInstance(el, { delay: 4000 }).show();
}

async function saveRow(btn) {
  const tr = btn.closest('tr');
  const body = {
    reason: tr.querySelector('.reason-input').value,
    new_due: tr.querySelector('.newdue-input').value
  };
  btn.disabled = true;
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  try {
    const res = await fetch('/api/tasks/' + tr.dataset.id, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.ok) toast('노션에 저장했습니다.'); else toast(data.error || '저장 실패', false);
  } catch (e) { toast('네트워크 오류: ' + e, false); }
  btn.disabled = false; btn.innerHTML = old;
}

async function previewNotify() {
  const body = document.getElementById('previewBody');
  body.textContent = '불러오는 중…';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('previewModal')).show();
  try {
    const res = await fetch('/api/notify/preview');
    const data = await res.json();
    if (!data.ok) { body.innerHTML = '<div class="alert alert-danger"></div>'; body.firstChild.textContent = data.error; return; }
    body.innerHTML = '';
    data.messages.forEach((m, i) => {
      const label = document.createElement('div');
      label.className = 'small text-muted mb-1';
      label.textContent = `메시지 ${i + 1} / ${data.messages.length} (${m.length}자)`;
      const pre = document.createElement('pre');
      pre.className = 'preview'; pre.textContent = m;
      body.append(label, pre);
    });
  } catch (e) { body.textContent = '오류: ' + e; }
}

async function sendNotify(fromModal = false) {
  if (!fromModal && !confirm('지연 업무를 담당자별로 디스코드에 전송할까요?')) return;
  const btn = document.getElementById('btnNotify');
  btn.disabled = true;
  try {
    const res = await fetch('/api/notify', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast(`디스코드 전송 완료 (지연 ${data.delayed}건, 메시지 ${data.messages}개)`);
      bootstrap.Modal.getInstance(document.getElementById('previewModal'))?.hide();
    } else toast(data.error || '전송 실패', false);
  } catch (e) { toast('네트워크 오류: ' + e, false); }
  btn.disabled = false;
}

function filterAll() {
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  const f = document.getElementById('statusFilter').value;
  document.querySelectorAll('#allTable tbody tr').forEach(tr => {
    const kind = tr.dataset.kind;
    const okStatus = f === 'all' || f === kind || (f === 'open' && kind !== 'done');
    const okText = !q || tr.dataset.search.includes(q);
    tr.style.display = okStatus && okText ? '' : 'none';
  });
}
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# 6. 실행
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    for msg in config_errors():
        log.warning(msg)
    log.info("대시보드 실행: http://%s:%s", "127.0.0.1" if HOST == "0.0.0.0" else HOST, PORT)
    app.run(host=HOST, port=PORT, debug=DEBUG)