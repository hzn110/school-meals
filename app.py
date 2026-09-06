import re
from datetime import date, timedelta
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(
    page_title="급식 반복성 분석",
    page_icon="🍚",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
NEIS_BASE_URL = "https://open.neis.go.kr/hub"
DEFAULT_SCHOOL = "당곡고등학교"
DEFAULT_OFFICE_CODE = "B10"  # 서울특별시교육청
DEFAULT_SCHOOL_CODE = "7010073"

# The key should be stored in Streamlit Secrets, not committed to GitHub.
# .streamlit/secrets.toml example:
# NEIS_API_KEY = "여기에_API_키"

def get_api_key() -> str:
    try:
        key = st.secrets.get("NEIS_API_KEY", "")
    except Exception:
        key = ""
    return str(key).strip()


API_KEY = get_api_key()

# ---------------------------------------------------------
# API helpers
# ---------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def neis_get(endpoint: str, params: dict) -> dict:
    if not API_KEY:
        raise RuntimeError("NEIS_API_KEY가 설정되지 않았습니다. Streamlit Secrets를 확인하세요.")

    request_params = {
        "KEY": API_KEY,
        "Type": "json",
        "pIndex": 1,
        "pSize": 1000,
        **params,
    }

    response = requests.get(
        f"{NEIS_BASE_URL}/{endpoint}",
        params=request_params,
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    # NEIS often returns RESULT instead of the expected dataset when an error occurs.
    if "RESULT" in data:
        code = data["RESULT"].get("CODE", "UNKNOWN")
        msg = data["RESULT"].get("MESSAGE", "알 수 없는 API 오류")
        raise RuntimeError(f"NEIS API 오류 ({code}): {msg}")

    return data


@st.cache_data(ttl=86400, show_spinner=False)
def search_schools(keyword: str, max_results: int = 30) -> pd.DataFrame:
    data = neis_get(
        "schoolInfo",
        {
            "ATPT_OFCDC_SC_CODE": "",
            "SCHUL_NM": keyword.strip(),
        },
    )

    rows = data.get("schoolInfo", [])
    if len(rows) < 2:
        return pd.DataFrame()

    result = pd.DataFrame(rows[1].get("row", []))
    if result.empty:
        return result

    # School type filter is intentionally broad enough for future use, but the
    # current project focuses on high schools.
    if "SCHUL_KND_SC_NM" in result.columns:
        high_mask = result["SCHUL_KND_SC_NM"].astype(str).str.contains("고등학교")
        high = result[high_mask].copy()
        if not high.empty:
            result = high

    cols = [
        "ATPT_OFCDC_SC_CODE",
        "ATPT_OFCDC_SC_NM",
        "SD_SCHUL_CODE",
        "SCHUL_NM",
        "LCTN_SC_NM",
        "ORG_RDNMA",
    ]
    cols = [c for c in cols if c in result.columns]
    result = result[cols].drop_duplicates(subset=["ATPT_OFCDC_SC_CODE", "SD_SCHUL_CODE"])
    return result.head(max_results)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_meals(
    office_code: str,
    school_code: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    data = neis_get(
        "mealServiceDietInfo",
        {
            "ATPT_OFCDC_SC_CODE": office_code,
            "SD_SCHUL_CODE": school_code,
            "MLSV_FROM_YMD": start_date.strftime("%Y%m%d"),
            "MLSV_TO_YMD": end_date.strftime("%Y%m%d"),
        },
    )

    rows = data.get("mealServiceDietInfo", [])
    if len(rows) < 2:
        return pd.DataFrame(columns=[
            "school", "date", "meal_type", "menu_raw", "menu_text", "calories"
        ])

    result = pd.DataFrame(rows[1].get("row", []))
    if result.empty:
        return pd.DataFrame(columns=[
            "school", "date", "meal_type", "menu_raw", "menu_text", "calories"
        ])

    result["date"] = pd.to_datetime(result["MLSV_YMD"], format="%Y%m%d", errors="coerce")
    result["meal_type"] = result.get("MMEAL_SC_NM", "중식")
    result["menu_raw"] = result.get("DDISH_NM", "")
    result["menu_text"] = result["menu_raw"].map(clean_menu_for_display)
    result["calories"] = pd.to_numeric(
        result.get("CAL_INFO", "").astype(str).str.extract(r"([\d.]+)")[0],
        errors="coerce",
    )
    result["school"] = result.get("SCHUL_NM", "알 수 없는 학교")

    return result[["school", "date", "meal_type", "menu_raw", "menu_text", "calories"]].dropna(subset=["date"])


# ---------------------------------------------------------
# Menu normalization / repeat analysis
# ---------------------------------------------------------
ALLERGY_RE = re.compile(r"\s*\([^)]*\)")
HTML_RE = re.compile(r"<[^>]+>")
MULTISPACE_RE = re.compile(r"\s+")


def clean_menu_for_display(menu: str) -> str:
    text = str(menu).replace("<br/>", "\n").replace("<br>", "\n")
    text = HTML_RE.sub("", text)
    return text.strip()


def normalize_menu(name: str) -> str:
    text = str(name)
    text = re.sub(r"\([^)]*\)", "", text)  # allergy numbers / notes
    text = re.sub(r"\[[^]]*\]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" -·•/,")
    return text


def split_menus(menu_text: str) -> List[str]:
    text = str(menu_text).replace("<br/>", "\n").replace("<br>", "\n")
    text = HTML_RE.sub("", text)
    parts = re.split(r"\n|<br ?/?>", text)
    cleaned: List[str] = []
    for part in parts:
        menu = normalize_menu(part)
        if menu and len(menu) >= 2:
            cleaned.append(menu)
    return cleaned


def explode_menu_data(meals: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in meals.iterrows():
        for menu in split_menus(row["menu_raw"]):
            rows.append(
                {
                    "school": row["school"],
                    "date": row["date"].date() if pd.notna(row["date"]) else None,
                    "meal_type": row["meal_type"],
                    "menu": menu,
                    "calories": row["calories"],
                }
            )
    return pd.DataFrame(rows)


def repeat_analysis(menu_df: pd.DataFrame) -> pd.DataFrame:
    if menu_df.empty:
        return pd.DataFrame(
            columns=["school", "menu", "appearances", "avg_gap", "min_gap", "max_gap"]
        )

    rows = []
    for (school, menu), group in menu_df.groupby(["school", "menu"]):
        dates = sorted(set(group["date"].dropna()))
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        rows.append(
            {
                "school": school,
                "menu": menu,
                "appearances": len(dates),
                "avg_gap": round(sum(gaps) / len(gaps), 1) if gaps else None,
                "min_gap": min(gaps) if gaps else None,
                "max_gap": max(gaps) if gaps else None,
            }
        )
    result = pd.DataFrame(rows)
    return result.sort_values(["school", "appearances", "menu"], ascending=[True, False, True])


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container {max-width: 1400px; padding-top: 2rem; padding-bottom: 3rem;}
    .hero {padding: 1.2rem 0 .8rem 0;}
    .hero h1 {margin-bottom: .2rem; font-size: 2.5rem;}
    .hero p {color: #666; font-size: 1.05rem;}
    .metric-label {font-size: .85rem; color: #666;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <h1>🍚 급식 반복성 분석</h1>
      <p>특정 메뉴는 얼마나 자주, 어떤 간격으로 다시 등장할까?</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if not API_KEY:
    st.error(
        "NEIS API 키가 설정되지 않았습니다. "
        "`.streamlit/secrets.toml`에 `NEIS_API_KEY = \"발급받은 키\"`를 추가하세요."
    )
    st.stop()

# Session state keeps user-added schools across reruns.
if "schools" not in st.session_state:
    st.session_state.schools = {
        DEFAULT_SCHOOL: {
            "office_code": DEFAULT_OFFICE_CODE,
            "school_code": DEFAULT_SCHOOL_CODE,
        }
    }

with st.sidebar:
    st.header("분석 설정")

    start_date = st.date_input(
        "분석 시작일",
        value=date.today() - timedelta(days=180),
    )
    end_date = st.date_input("분석 종료일", value=date.today())

    if start_date > end_date:
        st.error("시작일은 종료일보다 빠르거나 같아야 합니다.")
        st.stop()

    st.subheader("학교 추가")
    school_keyword = st.text_input("학교 이름 검색", placeholder="예: 서울고등학교")

    if st.button("학교 검색", use_container_width=True, disabled=not school_keyword.strip()):
        try:
            with st.spinner("학교를 찾는 중..."):
                results = search_schools(school_keyword)
            st.session_state.search_results = results
        except Exception as exc:
            st.error(str(exc))

    search_results = st.session_state.get("search_results", pd.DataFrame())
    if not search_results.empty:
        options = {
            f"{row.SCHUL_NM} · {row.LCTN_SC_NM if hasattr(row, 'LCTN_SC_NM') else ''}": idx
            for idx, row in search_results.iterrows()
        }
        selected_label = st.selectbox("검색 결과", list(options.keys()))
        selected_row = search_results.loc[options[selected_label]]

        if st.button("선택 학교 추가", use_container_width=True):
            st.session_state.schools[str(selected_row["SCHUL_NM"])] = {
                "office_code": str(selected_row["ATPT_OFCDC_SC_CODE"]),
                "school_code": str(selected_row["SD_SCHUL_CODE"]),
            }
            st.success(f"{selected_row['SCHUL_NM']} 추가됨")

    school_names = list(st.session_state.schools.keys())
    selected_schools = st.multiselect(
        "비교할 학교",
        options=school_names,
        default=[DEFAULT_SCHOOL],
        help="당곡고등학교가 기본 선택됩니다. 3개 이상의 학교를 선택하면 학교 간 비교가 활성화됩니다.",
    )

    st.caption(f"현재 등록 학교: {len(school_names)}개")

if not selected_schools:
    st.warning("분석할 학교를 1개 이상 선택하세요.")
    st.stop()

with st.spinner("NEIS 급식 데이터를 불러오는 중..."):
    meal_frames = []
    errors = []
    for school in selected_schools:
        info = st.session_state.schools[school]
        try:
            frame = fetch_meals(
                info["office_code"],
                info["school_code"],
                start_date,
                end_date,
            )
            # Use the selected label consistently even if API response contains a variation.
            frame["school"] = school
            meal_frames.append(frame)
        except Exception as exc:
            errors.append(f"{school}: {exc}")

if errors:
    for error in errors:
        st.error(error)

meals = pd.concat(meal_frames, ignore_index=True) if meal_frames else pd.DataFrame()
menu_df = explode_menu_data(meals) if not meals.empty else pd.DataFrame()
analysis_df = repeat_analysis(menu_df)

# ---------------------------------------------------------
# Overview cards
# ---------------------------------------------------------
meal_days = int(meals["date"].dt.date.nunique()) if not meals.empty else 0
unique_menus = int(menu_df["menu"].nunique()) if not menu_df.empty else 0
most_repeated = "-"
most_repeated_count = 0
if not analysis_df.empty:
    top = analysis_df.sort_values(["appearances", "menu"], ascending=[False, True]).iloc[0]
    most_repeated = str(top["menu"])
    most_repeated_count = int(top["appearances"])

c1, c2, c3, c4 = st.columns(4)
c1.metric("선택 학교", f"{len(selected_schools)}개")
c2.metric("급식 제공일", f"{meal_days:,}일")
c3.metric("고유 메뉴", f"{unique_menus:,}개")
c4.metric("가장 많이 반복된 메뉴", f"{most_repeated}", f"{most_repeated_count}회" if most_repeated != "-" else None)

st.divider()

if menu_df.empty:
    st.info("선택한 기간에 급식 데이터가 없습니다. 기간을 늘리거나 학교를 확인하세요.")
    st.stop()

# ---------------------------------------------------------
# Chart 1: top repeated menus
# ---------------------------------------------------------
st.subheader("① 어떤 메뉴가 가장 많이 반복되는가?")

school_for_top = st.selectbox("학교", selected_schools, key="top_school")
top_n = st.slider("표시할 메뉴 수", min_value=5, max_value=20, value=10)

top_df = (
    analysis_df[analysis_df["school"] == school_for_top]
    .sort_values(["appearances", "menu"], ascending=[False, True])
    .head(top_n)
    .copy()
)

fig_top = px.bar(
    top_df.sort_values("appearances"),
    x="appearances",
    y="menu",
    orientation="h",
    text="appearances",
    title=f"{school_for_top} 메뉴 등장 횟수 TOP {len(top_df)}",
    labels={"appearances": "등장 횟수", "menu": "메뉴"},
)
fig_top.update_traces(textposition="outside")
fig_top.update_layout(height=480, margin=dict(l=20, r=40, t=70, b=20))
st.plotly_chart(fig_top, use_container_width=True)

# ---------------------------------------------------------
# Chart 2: a selected menu over time
# ---------------------------------------------------------
st.subheader("② 특정 메뉴는 어떤 간격으로 다시 등장하는가?")

school_for_detail = st.selectbox("학교", selected_schools, key="detail_school")
detail_menus = sorted(menu_df.loc[menu_df["school"] == school_for_detail, "menu"].unique())
selected_menu = st.selectbox("메뉴 선택", detail_menus)

detail = (
    menu_df[
        (menu_df["school"] == school_for_detail)
        & (menu_df["menu"] == selected_menu)
    ]
    .drop_duplicates(subset=["date"])
    .sort_values("date")
    .copy()
)

dates = list(detail["date"])
gaps = [None]
for prev, curr in zip(dates, dates[1:]):
    gaps.append((curr - prev).days)
detail["gap"] = gaps

detail_metric = analysis_df[
    (analysis_df["school"] == school_for_detail)
    & (analysis_df["menu"] == selected_menu)
].iloc[0]

m1, m2, m3 = st.columns(3)
m1.metric("등장 횟수", f"{int(detail_metric['appearances'])}회")
m2.metric("평균 재등장 간격", f"{detail_metric['avg_gap']}일" if pd.notna(detail_metric["avg_gap"]) else "1회뿐")
m3.metric("최단 재등장 간격", f"{int(detail_metric['min_gap'])}일" if pd.notna(detail_metric["min_gap"]) else "-")

fig_detail = px.scatter(
    detail,
    x="date",
    y=[1] * len(detail),
    hover_data={"date": "|%Y-%m-%d", "gap": True, "y": False},
    labels={"date": "등장 날짜", "y": "등장"},
    title=f"{school_for_detail} · {selected_menu} 등장 시점",
)
fig_detail.update_yaxes(showticklabels=False, title=None, range=[0.8, 1.2])
fig_detail.update_traces(marker=dict(size=13))
fig_detail.update_layout(height=300, margin=dict(l=20, r=20, t=70, b=20))
st.plotly_chart(fig_detail, use_container_width=True)

# ---------------------------------------------------------
# Chart 3: school comparison, requiring 3+ selections
# ---------------------------------------------------------
st.subheader("③ 학교별 메뉴 반복성 비교")

if len(selected_schools) < 3:
    st.info("학교 비교 그래프는 3개 이상의 학교를 선택하면 표시됩니다.")
else:
    compare_df = (
        analysis_df.groupby("school", as_index=False)
        .agg(
            total_menu_appearances=("appearances", "sum"),
            repeated_menu_count=("appearances", lambda s: int((s >= 2).sum())),
            unique_menu_count=("menu", "nunique"),
        )
    )

    compare_long = compare_df.melt(
        id_vars="school",
        value_vars=["repeated_menu_count", "unique_menu_count"],
        var_name="metric",
        value_name="count",
    )
    compare_long["metric"] = compare_long["metric"].map(
        {
            "repeated_menu_count": "2회 이상 반복된 메뉴",
            "unique_menu_count": "고유 메뉴 수",
        }
    )

    fig_compare = px.bar(
        compare_long,
        x="school",
        y="count",
        color="metric",
        barmode="group",
        text="count",
        title="학교별 메뉴 반복성과 메뉴 다양성 비교",
        labels={"school": "학교", "count": "메뉴 수", "metric": "지표"},
    )
    fig_compare.update_layout(height=470, margin=dict(l=20, r=20, t=70, b=20))
    st.plotly_chart(fig_compare, use_container_width=True)

    st.caption("※ ‘반복된 메뉴’는 분석 기간에 같은 정규화 메뉴가 2회 이상 등장한 경우입니다.")

# ---------------------------------------------------------
# Detail table
# ---------------------------------------------------------
st.subheader("메뉴 반복성 데이터")

show_cols = analysis_df.rename(
    columns={
        "school": "학교",
        "menu": "메뉴",
        "appearances": "등장 횟수",
        "avg_gap": "평균 재등장 간격(일)",
        "min_gap": "최단 간격(일)",
        "max_gap": "최장 간격(일)",
    }
)
st.dataframe(
    show_cols.sort_values(["학교", "등장 횟수", "메뉴"], ascending=[True, False, True]),
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "분석 방식: NEIS의 급식 메뉴 문자열에서 알레르기 표기와 괄호 정보를 제거하고 메뉴명을 정규화한 뒤, 날짜별 등장 횟수와 재등장 간격을 계산합니다."
)
