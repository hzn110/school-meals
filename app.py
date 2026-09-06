import re
from datetime import date, timedelta
from typing import List

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


# =========================================================
# 페이지 설정
# =========================================================
st.set_page_config(
    page_title="급식 반복성 분석",
    page_icon="🍚",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =========================================================
# 기본 설정
# =========================================================
NEIS_BASE_URL = "https://open.neis.go.kr/hub"

DEFAULT_SCHOOL = "당곡고등학교"
DEFAULT_OFFICE_CODE = "B10"       # 서울특별시교육청
DEFAULT_SCHOOL_CODE = "7010073"   # 당곡고등학교


# =========================================================
# API KEY
# Streamlit Cloud에서는 Secrets에 등록
#
# [secrets.toml]
# NEIS_API_KEY = "발급받은_API_키"
# =========================================================
def get_api_key() -> str:
    try:
        return str(st.secrets.get("NEIS_API_KEY", "")).strip()
    except Exception:
        return ""


API_KEY = get_api_key()


# =========================================================
# CSS
# =========================================================
st.markdown(
    """
    <style>
    .block-container {
        max-width: 1400px;
        padding-top: 2rem;
        padding-bottom: 3rem;
    }

    .hero {
        padding: 1rem 0 1.2rem 0;
    }

    .hero h1 {
        margin-bottom: 0.2rem;
        font-size: 2.5rem;
    }

    .hero p {
        color: #777;
        font-size: 1.05rem;
        margin-top: 0;
    }

    .school-card {
        padding: 1rem;
        border-radius: 12px;
        border: 1px solid rgba(128,128,128,0.2);
        margin-bottom: 0.5rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# 제목
# =========================================================
st.markdown(
    """
    <div class="hero">
        <h1>🍚 급식 반복성 분석</h1>
        <p>특정 메뉴는 얼마나 자주, 어떤 간격으로 다시 등장할까?</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# API KEY 확인
# =========================================================
if not API_KEY:
    st.error(
        "NEIS API 키가 설정되지 않았습니다.\n\n"
        "Streamlit Secrets에 다음 형식으로 등록하세요:\n\n"
        '`NEIS_API_KEY = "발급받은_API_키"`'
    )
    st.stop()


# =========================================================
# 세션 상태
# =========================================================
if "schools" not in st.session_state:
    st.session_state.schools = {
        DEFAULT_SCHOOL: {
            "office_code": DEFAULT_OFFICE_CODE,
            "school_code": DEFAULT_SCHOOL_CODE,
        }
    }

if "selected_schools" not in st.session_state:
    st.session_state.selected_schools = [DEFAULT_SCHOOL]

if "search_results" not in st.session_state:
    st.session_state.search_results = pd.DataFrame()


# =========================================================
# NEIS 공통 호출
# =========================================================
@st.cache_data(ttl=3600, show_spinner=False)
def neis_get(endpoint: str, params: dict) -> dict:
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

    if "RESULT" in data:
        result = data["RESULT"]

        code = result.get("CODE", "UNKNOWN")
        message = result.get("MESSAGE", "알 수 없는 API 오류")

        raise RuntimeError(
            f"NEIS API 오류 ({code}): {message}"
        )

    return data


# =========================================================
# 학교 검색
# =========================================================
@st.cache_data(ttl=86400, show_spinner=False)
def search_schools(keyword: str, max_results: int = 50) -> pd.DataFrame:
    keyword = keyword.strip()

    if not keyword:
        return pd.DataFrame()

    data = neis_get(
        "schoolInfo",
        {
            "SCHUL_NM": keyword,
        },
    )

    rows = data.get("schoolInfo", [])

    if len(rows) < 2:
        return pd.DataFrame()

    result = pd.DataFrame(rows[1].get("row", []))

    if result.empty:
        return result

    # 고등학교 위주로 필터링
    if "SCHUL_KND_SC_NM" in result.columns:
        high_mask = result["SCHUL_KND_SC_NM"].astype(str).str.contains(
            "고등학교",
            na=False,
        )

        high = result[high_mask]

        if not high.empty:
            result = high

    needed_columns = [
        "ATPT_OFCDC_SC_CODE",
        "ATPT_OFCDC_SC_NM",
        "SD_SCHUL_CODE",
        "SCHUL_NM",
        "LCTN_SC_NM",
        "ORG_RDNMA",
    ]

    existing_columns = [
        col for col in needed_columns
        if col in result.columns
    ]

    result = result[existing_columns]

    result = result.drop_duplicates(
        subset=[
            "ATPT_OFCDC_SC_CODE",
            "SD_SCHUL_CODE",
        ]
    )

    return result.head(max_results)


# =========================================================
# 급식 데이터 가져오기
# =========================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_meals(
    office_code: str,
    school_code: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:

    empty_columns = [
        "school",
        "date",
        "meal_type",
        "menu_raw",
        "menu_text",
        "calories",
    ]

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
        return pd.DataFrame(columns=empty_columns)

    result = pd.DataFrame(rows[1].get("row", []))

    if result.empty:
        return pd.DataFrame(columns=empty_columns)

    # 날짜
    result["date"] = pd.to_datetime(
        result["MLSV_YMD"],
        format="%Y%m%d",
        errors="coerce",
    )

    # 중식 / 석식 등
    if "MMEAL_SC_NM" in result.columns:
        result["meal_type"] = result["MMEAL_SC_NM"].astype(str)
    else:
        result["meal_type"] = "중식"

    # 메뉴 원본
    if "DDISH_NM" in result.columns:
        result["menu_raw"] = result["DDISH_NM"].fillna("").astype(str)
    else:
        result["menu_raw"] = ""

    # 화면 표시용 메뉴
    result["menu_text"] = result["menu_raw"].map(
        clean_menu_for_display
    )

    # 칼로리
    if "CAL_INFO" in result.columns:
        calorie_text = result["CAL_INFO"].fillna("").astype(str)
        result["calories"] = pd.to_numeric(
            calorie_text.str.extract(r"([\d.]+)")[0],
            errors="coerce",
        )
    else:
        result["calories"] = None

    # 학교명
    if "SCHUL_NM" in result.columns:
        result["school"] = result["SCHUL_NM"].fillna("").astype(str)
    else:
        result["school"] = "알 수 없는 학교"

    return result[
        [
            "school",
            "date",
            "meal_type",
            "menu_raw",
            "menu_text",
            "calories",
        ]
    ].dropna(subset=["date"])


# =========================================================
# 메뉴 정리
# =========================================================
HTML_RE = re.compile(r"<[^>]+>")


def clean_menu_for_display(menu: str) -> str:
    text = str(menu)

    text = text.replace("<br/>", "\n")
    text = text.replace("<br>", "\n")

    text = HTML_RE.sub("", text)

    return text.strip()


def normalize_menu(name: str) -> str:
    text = str(name)

    # 알레르기 번호 및 괄호 내용 제거
    text = re.sub(r"\([^)]*\)", "", text)

    # 대괄호 내용 제거
    text = re.sub(r"\[[^]]*\]", "", text)

    # 공백 정리
    text = re.sub(r"\s+", " ", text)

    text = text.strip(" -·•/,")

    return text


def split_menus(menu_text: str) -> List[str]:
    text = str(menu_text)

    text = text.replace("<br/>", "\n")
    text = text.replace("<br>", "\n")

    text = HTML_RE.sub("", text)

    parts = re.split(
        r"\n|<br ?/?>",
        text,
    )

    menus = []

    for part in parts:
        menu = normalize_menu(part)

        if menu and len(menu) >= 2:
            menus.append(menu)

    return menus


# =========================================================
# 메뉴 단위 데이터로 변환
# =========================================================
def explode_menu_data(meals: pd.DataFrame) -> pd.DataFrame:
    if meals.empty:
        return pd.DataFrame(
            columns=[
                "school",
                "date",
                "meal_type",
                "menu",
                "calories",
            ]
        )

    rows = []

    for _, row in meals.iterrows():

        menus = split_menus(
            row["menu_raw"]
        )

        for menu in menus:
            rows.append(
                {
                    "school": row["school"],
                    "date": (
                        row["date"].date()
                        if pd.notna(row["date"])
                        else None
                    ),
                    "meal_type": row["meal_type"],
                    "menu": menu,
                    "calories": row["calories"],
                }
            )

    return pd.DataFrame(rows)


# =========================================================
# 반복성 분석
# =========================================================
def repeat_analysis(
    menu_df: pd.DataFrame,
) -> pd.DataFrame:

    columns = [
        "school",
        "menu",
        "appearances",
        "avg_gap",
        "min_gap",
        "max_gap",
    ]

    if menu_df.empty:
        return pd.DataFrame(columns=columns)

    result_rows = []

    for (school, menu), group in menu_df.groupby(
        ["school", "menu"]
    ):

        dates = sorted(
            set(
                group["date"]
                .dropna()
            )
        )

        gaps = [
            (b - a).days
            for a, b in zip(
                dates,
                dates[1:],
            )
        ]

        result_rows.append(
            {
                "school": school,
                "menu": menu,
                "appearances": len(dates),
                "avg_gap": (
                    round(
                        sum(gaps) / len(gaps),
                        1,
                    )
                    if gaps
                    else None
                ),
                "min_gap": (
                    min(gaps)
                    if gaps
                    else None
                ),
                "max_gap": (
                    max(gaps)
                    if gaps
                    else None
                ),
            }
        )

    result = pd.DataFrame(result_rows)

    return result.sort_values(
        [
            "school",
            "appearances",
            "menu",
        ],
        ascending=[
            True,
            False,
            True,
        ],
    )


# =========================================================
# 사이드바
# =========================================================
with st.sidebar:

    st.header("분석 설정")

    start_date = st.date_input(
        "분석 시작일",
        value=date.today() - timedelta(days=180),
    )

    end_date = st.date_input(
        "분석 종료일",
        value=date.today(),
    )

    if start_date > end_date:
        st.error(
            "시작일은 종료일보다 빠르거나 같아야 합니다."
        )
        st.stop()

    st.divider()

    # -----------------------------------------------------
    # 학교 검색
    # -----------------------------------------------------
    st.subheader("학교 추가")

    school_keyword = st.text_input(
        "학교 이름 검색",
        placeholder="예: 수도여자고등학교",
    )

    search_button = st.button(
        "학교 검색",
        use_container_width=True,
        disabled=not school_keyword.strip(),
    )

    if search_button:

        try:

            with st.spinner("학교를 찾는 중..."):

                results = search_schools(
                    school_keyword
                )

            st.session_state.search_results = results

            if results.empty:
                st.warning(
                    "검색 결과가 없습니다. "
                    "학교 이름을 정확하게 입력해 주세요."
                )

        except Exception as exc:

            st.error(
                f"학교 검색 중 오류가 발생했습니다.\n\n{exc}"
            )

    # -----------------------------------------------------
    # 검색 결과
    # -----------------------------------------------------
    search_results = st.session_state.search_results

    if not search_results.empty:

        result_labels = []
        result_map = {}

        for idx, row in search_results.iterrows():

            school_name = str(
                row.get(
                    "SCHUL_NM",
                    "",
                )
            )

            region = str(
                row.get(
                    "LCTN_SC_NM",
                    "",
                )
            )

            office_name = str(
                row.get(
                    "ATPT_OFCDC_SC_NM",
                    "",
                )
            )

            label = (
                f"{school_name} · "
                f"{region} · "
                f"{office_name}"
            ).strip(" ·")

            result_labels.append(label)
            result_map[label] = idx

        selected_result = st.selectbox(
            "검색 결과",
            result_labels,
        )

        selected_row = search_results.loc[
            result_map[selected_result]
        ]

        selected_school_name = str(
            selected_row["SCHUL_NM"]
        )

        if st.button(
            "+ 이 학교 추가",
            use_container_width=True,
        ):

            st.session_state.schools[
                selected_school_name
            ] = {
                "office_code": str(
                    selected_row[
                        "ATPT_OFCDC_SC_CODE"
                    ]
                ),
                "school_code": str(
                    selected_row[
                        "SD_SCHUL_CODE"
                    ]
                ),
            }

            current_selected = (
                st.session_state.selected_schools
            )

            if (
                selected_school_name
                not in current_selected
            ):
                st.session_state.selected_schools = (
                    current_selected
                    + [selected_school_name]
                )

            st.success(
                f"{selected_school_name} 추가 완료"
            )

    st.divider()

    # -----------------------------------------------------
    # 비교 학교 선택
    # -----------------------------------------------------
    school_names = list(
        st.session_state.schools.keys()
    )

    st.session_state.selected_schools = [
        school
        for school in
        st.session_state.selected_schools
        if school in school_names
    ]

    if not st.session_state.selected_schools:
        st.session_state.selected_schools = [
            DEFAULT_SCHOOL
        ]

    selected_schools = st.multiselect(
        "비교할 학교",
        options=school_names,
        default=st.session_state.selected_schools,
        key="school_selector",
        help=(
            "당곡고등학교가 기본 선택됩니다. "
            "학교 검색으로 학교를 추가한 뒤 "
            "3개 이상 선택할 수 있습니다."
        ),
    )

    st.session_state.selected_schools = (
        selected_schools
    )

    st.caption(
        f"현재 등록 학교: {len(school_names)}개"
    )


# =========================================================
# 선택 학교 확인
# =========================================================
if not selected_schools:

    st.warning(
        "분석할 학교를 1개 이상 선택하세요."
    )

    st.stop()


# =========================================================
# NEIS 데이터 불러오기
# =========================================================
with st.spinner(
    "NEIS 급식 데이터를 불러오는 중..."
):

    meal_frames = []
    errors = []

    for school in selected_schools:

        info = st.session_state.schools[
            school
        ]

        try:

            frame = fetch_meals(
                info["office_code"],
                info["school_code"],
                start_date,
                end_date,
            )

            # 화면에서 사용하는 학교명으로 통일
            frame["school"] = school

            meal_frames.append(frame)

        except Exception as exc:

            errors.append(
                f"{school}: {exc}"
            )


# =========================================================
# API 오류 표시
# =========================================================
if errors:

    for error in errors:
        st.error(error)


# =========================================================
# 데이터 통합
# =========================================================
if meal_frames:

    meals = pd.concat(
        meal_frames,
        ignore_index=True,
    )

else:

    meals = pd.DataFrame()


menu_df = (
    explode_menu_data(meals)
    if not meals.empty
    else pd.DataFrame()
)

analysis_df = repeat_analysis(menu_df)


# =========================================================
# 분석 데이터 없음
# =========================================================
if menu_df.empty:

    st.info(
        "선택한 기간에 급식 데이터가 없습니다. "
        "분석 기간을 늘리거나 학교를 확인하세요."
    )

    st.stop()


# =========================================================
# 요약 지표
# =========================================================
meal_days = (
    int(
        meals["date"]
        .dt.date
        .nunique()
    )
    if not meals.empty
    else 0
)

unique_menus = (
    int(
        menu_df["menu"]
        .nunique()
    )
    if not menu_df.empty
    else 0
)

most_repeated = "-"
most_repeated_count = 0

if not analysis_df.empty:

    top = (
        analysis_df
        .sort_values(
            [
                "appearances",
                "menu",
            ],
            ascending=[
                False,
                True,
            ],
        )
        .iloc[0]
    )

    most_repeated = str(
        top["menu"]
    )

    most_repeated_count = int(
        top["appearances"]
    )


# =========================================================
# 상단 카드
# =========================================================
c1, c2, c3, c4 = st.columns(4)

c1.metric(
    "선택 학교",
    f"{len(selected_schools)}개",
)

c2.metric(
    "급식 제공일",
    f"{meal_days:,}일",
)

c3.metric(
    "고유 메뉴",
    f"{unique_menus:,}개",
)

c4.metric(
    "가장 많이 반복된 메뉴",
    most_repeated,
    (
        f"{most_repeated_count}회"
        if most_repeated != "-"
        else None
    ),
)

st.divider()


# =========================================================
# ① 메뉴 반복 TOP
# =========================================================
st.subheader(
    "① 어떤 메뉴가 가장 많이 반복되는가?"
)

school_for_top = st.selectbox(
    "분석할 학교",
    selected_schools,
    key="top_school",
)

top_n = st.slider(
    "표시할 메뉴 수",
    min_value=5,
    max_value=20,
    value=10,
)

top_df = (
    analysis_df[
        analysis_df["school"]
        == school_for_top
    ]
    .sort_values(
        [
            "appearances",
            "menu",
        ],
        ascending=[
            False,
            True,
        ],
    )
    .head(top_n)
    .copy()
)

fig_top = px.bar(
    top_df.sort_values(
        "appearances"
    ),
    x="appearances",
    y="menu",
    orientation="h",
    text="appearances",
    title=(
        f"{school_for_top} "
        f"메뉴 등장 횟수 TOP {len(top_df)}"
    ),
    labels={
        "appearances": "등장 횟수",
        "menu": "메뉴",
    },
)

fig_top.update_traces(
    textposition="outside"
)

fig_top.update_layout(
    height=480,
    margin=dict(
        l=20,
        r=40,
        t=70,
        b=20,
    ),
)

st.plotly_chart(
    fig_top,
    use_container_width=True,
)


# =========================================================
# ② 특정 메뉴 등장 시점
# =========================================================
st.subheader(
    "② 특정 메뉴는 어떤 간격으로 다시 등장하는가?"
)

school_for_detail = st.selectbox(
    "학교",
    selected_schools,
    key="detail_school",
)

detail_menus = sorted(
    menu_df.loc[
        menu_df["school"]
        == school_for_detail,
        "menu",
    ]
    .unique()
)

selected_menu = st.selectbox(
    "메뉴 선택",
    detail_menus,
)


detail = (
    menu_df[
        (menu_df["school"] == school_for_detail)
        & (menu_df["menu"] == selected_menu)
    ]
    .drop_duplicates(
        subset=["date"]
    )
    .sort_values("date")
    .copy()
)


dates = list(
    detail["date"]
)

gaps = [None]

for previous, current in zip(
    dates,
    dates[1:],
):

    gaps.append(
        (current - previous).days
    )

detail["gap"] = gaps
detail["occurrence"] = 1


detail_metric = analysis_df[
    (analysis_df["school"] == school_for_detail)
    & (analysis_df["menu"] == selected_menu)
]

if not detail_metric.empty:

    detail_metric = detail_metric.iloc[0]

    m1, m2, m3 = st.columns(3)

    m1.metric(
        "등장 횟수",
        f"{int(detail_metric['appearances'])}회",
    )

    if pd.notna(detail_metric["avg_gap"]):

        m2.metric(
            "평균 재등장 간격",
            f"{detail_metric['avg_gap']}일",
        )

    else:

        m2.metric(
            "평균 재등장 간격",
            "1회뿐",
        )

    if pd.notna(detail_metric["min_gap"]):

        m3.metric(
            "최단 재등장 간격",
            f"{int(detail_metric['min_gap'])}일",
        )

    else:

        m3.metric(
            "최단 재등장 간격",
            "-",
        )


fig_detail = px.scatter(
    detail,
    x="date",
    y="occurrence",
    hover_data={
        "date": "|%Y-%m-%d",
        "gap": True,
        "occurrence": False,
    },
    labels={
        "date": "등장 날짜",
        "occurrence": "등장",
    },
    title=(
        f"{school_for_detail} · "
        f"{selected_menu} 등장 시점"
    ),
)

fig_detail.update_yaxes(
    showticklabels=False,
    title=None,
    range=[0.8, 1.2],
)

fig_detail.update_traces(
    marker=dict(size=13)
)

fig_detail.update_layout(
    height=300,
    margin=dict(
        l=20,
        r=20,
        t=70,
        b=20,
    ),
)

st.plotly_chart(
    fig_detail,
    use_container_width=True,
)


# =========================================================
# ③ 학교별 비교
# =========================================================
st.subheader(
    "③ 학교별 메뉴 반복성 비교"
)

if len(selected_schools) < 3:

    st.info(
        "학교 비교 그래프는 "
        "3개 이상의 학교를 선택하면 표시됩니다."
    )

else:

    # -----------------------------------------------------
    # 학교별 반복성 통계
    # -----------------------------------------------------
    compare_df = (
        analysis_df
        .groupby("school", as_index=False)
        .agg(
            total_menu_appearances=(
                "appearances",
                "sum",
            ),
            repeated_menu_count=(
                "appearances",
                lambda s: int(
                    (s >= 2).sum()
                ),
            ),
            unique_menu_count=(
                "menu",
                "nunique",
            ),
        )
    )

    # -----------------------------------------------------
    # ★ 학교별 가장 많이 나온 메뉴
    # -----------------------------------------------------
    favorite_rows = (
        analysis_df
        .sort_values(
            [
                "school",
                "appearances",
                "menu",
            ],
            ascending=[
                True,
                False,
                True,
            ],
        )
        .drop_duplicates(
            subset=["school"],
            keep="first",
        )
        [
            [
                "school",
                "menu",
                "appearances",
            ]
        ]
        .rename(
            columns={
                "school": "학교",
                "menu": "가장 많이 나온 메뉴",
                "appearances": "등장 횟수",
            }
        )
    )

    st.markdown(
        "### 학교별 최다 등장 메뉴"
    )

    st.dataframe(
        favorite_rows,
        use_container_width=True,
        hide_index=True,
    )

    # -----------------------------------------------------
    # 학교별 반복성 그래프
    # -----------------------------------------------------
    compare_long = compare_df.melt(
        id_vars="school",
        value_vars=[
            "repeated_menu_count",
            "unique_menu_count",
        ],
        var_name="metric",
        value_name="count",
    )

    compare_long["metric"] = (
        compare_long["metric"]
        .map(
            {
                "repeated_menu_count":
                    "2회 이상 반복된 메뉴",
                "unique_menu_count":
                    "고유 메뉴 수",
            }
        )
    )

    fig_compare = px.bar(
        compare_long,
        x="school",
        y="count",
        color="metric",
        barmode="group",
        text="count",
        title="학교별 메뉴 반복성과 메뉴 다양성 비교",
        labels={
            "school": "학교",
            "count": "메뉴 수",
            "metric": "지표",
        },
    )

    fig_compare.update_layout(
        height=470,
        margin=dict(
            l=20,
            r=20,
            t=70,
            b=20,
        ),
    )

    st.plotly_chart(
        fig_compare,
        use_container_width=True,
    )

    st.caption(
        "※ 반복 메뉴는 분석 기간 동안 "
        "같은 정규화 메뉴가 2회 이상 등장한 경우입니다."
    )


# =========================================================
# 전체 반복성 데이터
# =========================================================
st.subheader(
    "메뉴 반복성 데이터"
)

show_cols = (
    analysis_df
    .rename(
        columns={
            "school": "학교",
            "menu": "메뉴",
            "appearances": "등장 횟수",
            "avg_gap": "평균 재등장 간격(일)",
            "min_gap": "최단 간격(일)",
            "max_gap": "최장 간격(일)",
        }
    )
)

show_cols = show_cols.sort_values(
    [
        "학교",
        "등장 횟수",
        "메뉴",
    ],
    ascending=[
        True,
        False,
        True,
    ],
)

st.dataframe(
    show_cols,
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "분석 방식: NEIS 급식 데이터의 메뉴에서 "
    "알레르기 표기 및 괄호 정보를 제거하고 메뉴명을 정규화한 뒤, "
    "메뉴별 등장 횟수와 재등장 간격을 계산합니다."
)
