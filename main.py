"""
===============================================================================
 노션 워크파일 지연 업무 대시보드 (Streamlit 버전)
===============================================================================

■ 기능
  1) 메인 대시보드 : 노션 DB의 전체 업무 + 오늘 기준 '지연 업무'를 표로 표시
  2) 지연 건 요약   : 마감일이 지났고 '완료'가 아닌 항목을 자동 필터링해 상단 지표로 요약
  3) 디스코드 알림  : [디스코드 알림 전송] 버튼 → 담당자별로 묶어 웹훅 전송 (미리보기 지원)
  4) 지연 사유/변경 일정 입력 : 표에서 바로 수정 → [노션에 저장] 누르면 노션 페이지에 반영

■ 폴더 구조
  notion-project/
   ├─ main.py           ← 이 파일
   └─ .env              ← 토큰·웹훅 등 설정 (아래 예시 참고)

■ 설치 (VS Code 터미널에서, Python 3.9 이상)
  python -m pip install streamlit notion-client requests python-dotenv tzdata
  # 'python' 이 안 되면 'py -m pip install ...' 로 실행

■ 실행  ※ 'python main.py' 가 아니라 아래 명령으로 실행해야 합니다!
  python -m streamlit run main.py
  → 브라우저가 자동으로 열립니다 (안 열리면 http://localhost:8501 접속)
  → 종료는 터미널에서 Ctrl + C

■ .env 파일 예시 (main.py 와 같은 폴더에 ".env" 라는 이름으로 저장)
  NOTION_TOKEN=ntn_xxxxxxxxxxxxxxxxxxxx
  NOTION_DATABASE_ID=노션 DB URL 통째로 또는 32자리 ID
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  DASHBOARD_URL=                       # (선택) 디스코드 메시지 하단에 붙일 주소
  PROP_TITLE=업무명
  PROP_ASSIGNEE=담당자
  PROP_DUE=마감일
  PROP_STATUS=상태
  PROP_REASON=지연 사유
  PROP_NEW_DUE=변경 일정
  DONE_STATUSES=완료,Done
  USE_NEW_DUE_FOR_DELAY=true
  DISCORD_MENTIONS={"홍길동": "123456789012345678"}
  TIMEZONE=Asia/Seoul

■ 노션 준비
  1) https://www.notion.so/profile/integrations → [새 API 통합] → "내부 통합 시크릿" 복사 → NOTION_TOKEN
  2) 워크파일 DB 페이지 오른쪽 위 [⋯] → [연결] → 만든 통합 추가 (안 하면 404 오류)
  3) DB 속성 이름이 기본값과 다르면 .env 의 PROP_* 를 실제 컬럼명으로 변경
  4) 입력 기능을 쓰려면 DB에 '지연 사유'(텍스트), '변경 일정'(날짜) 속성 추가

■ 디스코드 준비
  채널 설정(⚙) → [연동] → [웹후크] → [새 웹후크] → [웹후크 URL 복사] → DISCORD_WEBHOOK_URL
  (선택) 실제 @멘션: 디스코드 [설정 → 고급 → 개발자 모드] 켜고 사용자 우클릭 → [ID 복사]

■ 지연 판정 규칙
  - 기준일 = 오늘 (TIMEZONE)
  - 상태가 DONE_STATUSES 에 해당하면 지연 아님 (체크박스 속성이면 체크 = 완료)
  - USE_NEW_DUE_FOR_DELAY=true 이면 '변경 일정'이 있는 업무는 변경 일정 기준으로 판단
  - 기준 날짜가 오늘보다 이전이면 지연 (오늘 마감은 지연 아님)
===============================================================================
"""

import json
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from notion_client import APIResponseError, Client

# -----------------------------------------------------------------------------
# 1. 환경 변수
# -----------------------------------------------------------------------------
load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID_RAW = os.getenv("NOTION_DATABASE_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Seoul")

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

UNASSIGNED = "미지정"
DISCORD_LIMIT = 1900  # 디스코드 메시지 최대 2000자 → 여유 있게
CACHE_SECONDS = 60    # 노션 조회 결과를 60초간 재사용 (새로고침 버튼으로 즉시 갱신 가능)


def normalize_notion_id(raw: str) -> str:
    """URL 이나 하이픈 섞인 ID 에서 32자리 노션 ID 만 뽑아낸다."""
    if not raw:
        return ""
    last = raw.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
    compact = last.replace("-", "")
    found = re.search(r"[0-9a-fA-F]{32}$", compact)
    return found.group(0) if found else raw


NOTION_DATABASE_ID = normalize_notion_id(NOTION_DATABASE_ID_RAW)


# -----------------------------------------------------------------------------
# 2. 노션 조회 / 파싱
# -----------------------------------------------------------------------------
@st.cache_resource
def get_notion():
    # notion_version 고정 → notion-client 버전이 바뀌어도 동일하게 동작
    return Client(auth=NOTION_TOKEN, notion_version="2022-06-28")


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="노션에서 업무를 불러오는 중…")
def fetch_pages() -> list:
    """DB 의 모든 페이지를 100개 단위로 페이지네이션하며 가져온다."""
    notion = get_notion()
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


@st.cache_data(ttl=600, show_spinner=False)
def fetch_schema() -> dict:
    """DB 속성 목록 {속성이름: 타입}."""
    db = get_notion().request(path=f"databases/{NOTION_DATABASE_ID}", method="GET")
    return {name: p.get("type") for name, p in db.get("properties", {}).items()}


def today_local() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _person_name(p: dict) -> str:
    return (p or {}).get("name") or "(이름없음)"


def prop_to_text(prop: dict) -> str:
    """어떤 타입의 노션 속성이든 문자열로 변환."""
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
    return [s.strip() for s in re.split(r"[,/]", prop_to_text(prop)) if s.strip()]


def parse_date(s: str):
    """'2026-09-30' 또는 '2026-09-30T10:00:00.000+09:00' → date (현지 시간대)."""
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

    title_prop = props.get(PROP_TITLE)
    if not title_prop or title_prop.get("type") != "title":  # 제목 속성 자동 탐지
        title_prop = next((p for p in props.values() if p.get("type") == "title"), None)

    status_prop = props.get(PROP_STATUS)
    status = prop_to_text(status_prop)
    if status_prop and status_prop.get("type") == "checkbox":
        is_done = bool(status_prop.get("checkbox"))
    else:
        is_done = status in DONE_STATUSES

    due = parse_date(prop_to_text(props.get(PROP_DUE)))
    new_due = parse_date(prop_to_text(props.get(PROP_NEW_DUE)))
    effective_due = (new_due or due) if USE_NEW_DUE_FOR_DELAY else due
    is_delayed = bool(effective_due and not is_done and effective_due < today)

    assignees = prop_to_list(props.get(PROP_ASSIGNEE)) or [UNASSIGNED]
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "title": prop_to_text(title_prop) or "(제목 없음)",
        "assignees": assignees,
        "assignee_text": ", ".join(assignees),
        "status": status or "-",
        "is_done": is_done,
        "due": due,
        "new_due": new_due,
        "effective_due": effective_due,
        "reason": prop_to_text(props.get(PROP_REASON)),
        "is_delayed": is_delayed,
        "overdue_days": (today - effective_due).days if is_delayed else 0,
    }


def load_tasks():
    today = today_local()
    tasks = [parse_page(p, today) for p in fetch_pages()]
    tasks.sort(key=lambda t: (t["effective_due"] or date.max, t["title"]))
    delayed = sorted([t for t in tasks if t["is_delayed"]], key=lambda t: -t["overdue_days"])
    return tasks, delayed, today


def group_by_assignee(delayed: list) -> dict:
    """담당자별로 묶기 (담당자가 여럿이면 각자에게 포함, 건수 많은 순, 미지정은 맨 뒤)."""
    grouped = defaultdict(list)
    for t in delayed:
        for a in t["assignees"]:
            grouped[a].append(t)
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == UNASSIGNED, -len(kv[1]), kv[0])))


def notion_error_message(e: Exception) -> str:
    if isinstance(e, APIResponseError):
        code = getattr(e, "code", "")
        if code == "object_not_found":
            return "노션 DB를 찾을 수 없습니다. DB ID가 맞는지, DB에 통합(Integration) 연결을 추가했는지 확인하세요."
        if code == "unauthorized":
            return "노션 토큰이 올바르지 않습니다. NOTION_TOKEN 을 확인하세요."
        return f"노션 API 오류 ({code}): {e}"
    return f"오류: {e}"


def update_notion_page(page_id: str, reason, new_due, schema: dict) -> None:
    """지연 사유 / 변경 일정을 노션 페이지에 저장."""
    props = {}
    if reason is not None:
        reason = str(reason).strip()[:2000]
        if schema.get(PROP_REASON) == "select":
            props[PROP_REASON] = {"select": {"name": reason[:100]} if reason else None}
        else:
            props[PROP_REASON] = {"rich_text": [{"type": "text", "text": {"content": reason}}] if reason else []}
    if new_due is not False:  # False = 변경 없음, None = 비우기
        props[PROP_NEW_DUE] = {"date": {"start": new_due.isoformat()} if new_due else None}
    if props:
        get_notion().pages.update(page_id=page_id, properties=props)


# -----------------------------------------------------------------------------
# 3. 디스코드
# -----------------------------------------------------------------------------
def mention_of(name: str) -> str:
    uid = DISCORD_MENTIONS.get(name)
    return f"<@{uid}>" if uid else f"**{name}**"


def build_discord_messages(delayed: list, today: date) -> list:
    """담당자별로 정리된 디스코드 메시지 목록(각 2000자 이하)."""
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

    messages, current = [], header
    for block in blocks:
        if len(block) > DISCORD_LIMIT:  # 한 사람 분량이 너무 길면 줄 단위로 분할
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
            time.sleep(float(r.json().get("retry_after", 1)))
            continue
        r.raise_for_status()
        return
    raise RuntimeError("디스코드 속도 제한으로 전송에 실패했습니다.")


# -----------------------------------------------------------------------------
# 4. 화면 (Streamlit)
# -----------------------------------------------------------------------------
def to_date(v):
    """data_editor 가 돌려준 값(date/Timestamp/문자열/NaT) → date 또는 None."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.date()
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return parse_date(str(v))


def clean_text(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def refresh():
    st.cache_data.clear()
    st.session_state.editor_ver = st.session_state.get("editor_ver", 0) + 1


st.set_page_config(page_title="워크파일 일정 대시보드", page_icon="📋", layout="wide")
st.session_state.setdefault("editor_ver", 0)

# ----- 사이드바 -----
with st.sidebar:
    st.header("⚙️ 설정 상태")
    st.write(("✅" if NOTION_TOKEN else "❌") + " 노션 토큰")
    st.write(("✅" if NOTION_DATABASE_ID else "❌") + " 노션 DB ID")
    st.write(("✅" if DISCORD_WEBHOOK_URL else "❌") + " 디스코드 웹훅")
    st.caption(
        f"완료 상태값: {', '.join(sorted(DONE_STATUSES))}  \n"
        f"지연 기준: {'변경 일정 우선' if USE_NEW_DUE_FOR_DELAY else '원래 마감일'}  \n"
        f"자동 새로고침: {CACHE_SECONDS}초 캐시"
    )
    if st.button("🔄 노션 새로고침", width="stretch"):
        refresh()
        st.rerun()

st.title("📋 워크파일 일정 대시보드")

# ----- 설정 / 데이터 로드 -----
missing = [n for n, v in [("NOTION_TOKEN", NOTION_TOKEN), ("NOTION_DATABASE_ID", NOTION_DATABASE_ID)] if not v]
if missing:
    st.error(f"{', '.join(missing)} 가 비어 있습니다. main.py 와 같은 폴더의 .env 파일을 확인하세요.")
    st.stop()

try:
    tasks, delayed, today = load_tasks()
    schema = fetch_schema()
except Exception as e:  # noqa: BLE001
    st.error(notion_error_message(e))
    st.stop()

st.caption(f"기준일 {today.isoformat()} · 노션 업무 {len(tasks)}건")

for label, name in [("담당자", PROP_ASSIGNEE), ("마감일", PROP_DUE), ("상태", PROP_STATUS)]:
    if name not in schema:
        st.warning(f"DB에 '{name}' 속성이 없습니다 ({label}). .env 의 PROP_* 이름을 실제 컬럼명과 맞춰 주세요.")
can_edit = PROP_REASON in schema and schema.get(PROP_NEW_DUE) == "date"
if not can_edit:
    st.info(f"지연 사유/변경 일정 입력을 쓰려면 DB에 '{PROP_REASON}'(텍스트), '{PROP_NEW_DUE}'(날짜) 속성을 추가하세요.")

grouped = group_by_assignee(delayed)

# ----- 요약 지표 -----
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("전체 업무", len(tasks))
c2.metric("완료", sum(1 for t in tasks if t["is_done"]))
c3.metric("🚨 일정 지연", len(delayed))
c4.metric("지연 담당자", f"{len(grouped)}명")
c5.metric("최장 지연", f"D+{max((t['overdue_days'] for t in delayed), default=0)}")

# ----- 담당자별 지연 현황 + 디스코드 -----
left, right = st.columns([3, 2])
with left:
    st.subheader("👥 담당자별 지연 현황")
    if grouped:
        st.bar_chart(
            pd.DataFrame({"지연 건수": [len(v) for v in grouped.values()]}, index=list(grouped.keys())),
            horizontal=True,
            height=max(160, 40 * len(grouped)),
        )
    else:
        st.success("지연된 업무가 없습니다 🎉")

with right:
    st.subheader("📣 디스코드 알림")
    messages = build_discord_messages(delayed, today)
    with st.expander(f"전송 미리보기 (메시지 {len(messages)}개)"):
        for i, m in enumerate(messages, 1):
            st.caption(f"메시지 {i} / {len(messages)} · {len(m)}자")
            st.code(m, language=None)
    if not DISCORD_WEBHOOK_URL:
        st.warning("DISCORD_WEBHOOK_URL 이 설정되지 않았습니다.")
    confirm = st.checkbox("위 내용으로 전송할게요", disabled=not DISCORD_WEBHOOK_URL)
    if st.button("🚀 디스코드 알림 전송", type="primary", width="stretch",
                 disabled=not (DISCORD_WEBHOOK_URL and confirm)):
        try:
            with st.spinner("디스코드로 전송 중…"):
                st.cache_data.clear()                      # 전송 직전 최신 데이터로 다시 조회
                _, fresh_delayed, fresh_today = load_tasks()
                fresh_msgs = build_discord_messages(fresh_delayed, fresh_today)
                for i, msg in enumerate(fresh_msgs):
                    post_to_discord(msg)
                    if i < len(fresh_msgs) - 1:
                        time.sleep(0.6)                    # 연속 전송 속도 제한 방지
            st.success(f"전송 완료! (지연 {len(fresh_delayed)}건, 메시지 {len(fresh_msgs)}개)")
        except requests.HTTPError as e:
            st.error(f"디스코드 전송 실패: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:  # noqa: BLE001
            st.error(notion_error_message(e))

st.divider()

# ----- 탭: 지연 업무 / 전체 업무 -----
tab_delayed, tab_all = st.tabs([f"🚨 지연 업무 ({len(delayed)})", f"📄 전체 업무 ({len(tasks)})"])

with tab_delayed:
    if not delayed:
        st.success("지연된 업무가 없습니다 🎉")
    else:
        df = pd.DataFrame(
            [{
                "업무명": t["title"],
                "담당자": t["assignee_text"],
                "상태": t["status"],
                "마감일": t["due"],
                "지연": f"D+{t['overdue_days']}",
                PROP_REASON: t["reason"],
                PROP_NEW_DUE: t["new_due"],
                "노션": t["url"],
            } for t in delayed],
            index=[t["id"] for t in delayed],
        )
        st.caption("✏️ '지연 사유'와 '변경 일정' 칸을 더블클릭해 수정한 뒤 아래 [노션에 저장]을 누르세요.")
        edited = st.data_editor(
            df,
            key=f"delayed_editor_{st.session_state.editor_ver}",
            width="stretch",
            hide_index=True,
            disabled=["업무명", "담당자", "상태", "마감일", "지연", "노션"] + ([] if can_edit else [PROP_REASON, PROP_NEW_DUE]),
            column_config={
                "마감일": st.column_config.DateColumn(format="YYYY-MM-DD"),
                PROP_REASON: st.column_config.TextColumn(width="large", max_chars=2000),
                PROP_NEW_DUE: st.column_config.DateColumn(format="YYYY-MM-DD"),
                "노션": st.column_config.LinkColumn(display_text="열기"),
            },
        )

        # 바뀐 행만 골라내기
        changes = []
        for pid in df.index:
            old_r, new_r = clean_text(df.at[pid, PROP_REASON]), clean_text(edited.at[pid, PROP_REASON])
            old_d, new_d = to_date(df.at[pid, PROP_NEW_DUE]), to_date(edited.at[pid, PROP_NEW_DUE])
            if old_r != new_r or old_d != new_d:
                changes.append((pid, df.at[pid, "업무명"],
                                new_r if old_r != new_r else None,
                                new_d if old_d != new_d else False))

        if st.button(f"💾 노션에 저장 ({len(changes)}건 변경)", type="primary",
                     disabled=not (can_edit and changes)):
            ok, fail = 0, []
            with st.spinner("노션에 저장 중…"):
                for pid, title, reason, new_due in changes:
                    try:
                        update_notion_page(pid, reason, new_due, schema)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        fail.append(f"{title}: {notion_error_message(e)}")
            if fail:
                st.error("일부 저장 실패\n\n" + "\n\n".join(fail))
            else:
                st.toast(f"노션에 {ok}건 저장했습니다 ✅")
                refresh()
                time.sleep(0.8)
                st.rerun()

with tab_all:
    f1, f2 = st.columns([3, 1])
    q = f1.text_input("검색", placeholder="업무명·담당자 검색", label_visibility="collapsed")
    kind = f2.selectbox("구분", ["전체", "지연만", "미완료만", "완료만"], label_visibility="collapsed")

    def kind_of(t):
        return "완료" if t["is_done"] else (f"🚨 지연 D+{t['overdue_days']}" if t["is_delayed"] else "진행")

    rows = []
    for t in tasks:
        if q and q.lower() not in (t["title"] + " " + t["assignee_text"]).lower():
            continue
        if kind == "지연만" and not t["is_delayed"]:
            continue
        if kind == "미완료만" and t["is_done"]:
            continue
        if kind == "완료만" and not t["is_done"]:
            continue
        rows.append({
            "구분": kind_of(t),
            "업무명": t["title"],
            "담당자": t["assignee_text"],
            "상태": t["status"],
            "마감일": t["due"],
            "변경 일정": t["new_due"],
            "지연 사유": t["reason"],
            "노션": t["url"],
        })

    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "마감일": st.column_config.DateColumn(format="YYYY-MM-DD"),
            "변경 일정": st.column_config.DateColumn(format="YYYY-MM-DD"),
            "노션": st.column_config.LinkColumn(display_text="열기"),
        },
    )
    st.caption(f"{len(rows)}건 표시")
