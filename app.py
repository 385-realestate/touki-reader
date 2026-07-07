"""
登記簿ビューアー — Streamlit版 v1.7
GitHub + Streamlit Cloud デプロイ対応
"""
import sys
import io
import csv
import re
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components
from streamlit_js_eval import streamlit_js_eval

# scriptsパスを追加（ローカル・Streamlit Cloud両対応）
SCRIPTS_DIR = Path(__file__).parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from touki_parser import extract_text, zen2han, detect_type, file_md5
from agents.tochi_agent import TochiAgent
from agents.tatemono_agent import TatemonoAgent

SEP = "；"

# ─── ページ設定 ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="登記簿 PDF パーサー v1.7",
    page_icon="📋",
    layout="wide",
)

# ── パスワード認証（localStorage永続化）──────────────────────
AUTH_TOKEN = "touki_auth_ok"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "save_token" not in st.session_state:
    st.session_state.save_token = False

# ログイン成功後の次レンダリングでlocalStorageに保存
# （st.rerun()より前に実行するとJSが走る前にページが切り替わるため分離）
if st.session_state.save_token:
    streamlit_js_eval(
        js_expressions=f"localStorage.setItem('auth_token', '{AUTH_TOKEN}')",
        key="save_auth"
    )
    st.session_state.save_token = False

# localStorageに認証済みトークンがあれば自動ログイン
if not st.session_state.authenticated:
    saved = streamlit_js_eval(
        js_expressions="localStorage.getItem('auth_token')",
        key="check_auth"
    )
    if saved == AUTH_TOKEN:
        st.session_state.authenticated = True

if not st.session_state.authenticated:
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@600;800&family=Noto+Sans+JP:wght@400;500;700&family=JetBrains+Mono:wght@500&display=swap');

    .block-container { max-width: 640px !important; padding-top: 44px !important; }
    [data-testid="stVerticalBlock"] { gap: 0.35rem !important; }
    .stTextInput [data-baseweb="input"] + div,
    small.st-emotion-cache-1dp5vir,
    [data-testid="InputInstructions"] { display: none !important; }
    [data-testid="stStatusWidget"] { display: none !important; }
    #MainMenu, header, footer { display: none !important; }

    .gate-card { font-family: 'Noto Sans JP', sans-serif;
        border-radius: 14px; box-shadow: 0 14px 36px rgba(27,47,94,0.18);
        overflow: hidden; margin-bottom: 22px; }

    .gate-band {
        position: relative;
        background: linear-gradient(155deg, #1B2F5E 0%, #12234A 100%);
        padding: 40px 46px 32px; color: #fff; overflow: hidden;
    }
    .gate-band::after {
        content: ""; position: absolute; inset: 0;
        background: repeating-linear-gradient(0deg, rgba(255,255,255,0.035) 0 1px, transparent 1px 28px);
        pointer-events: none;
    }
    .gate-eyebrow {
        font-family: 'JetBrains Mono', monospace; font-size: 11px;
        letter-spacing: 0.16em; color: #7C93C4; margin-bottom: 10px;
    }
    .gate-title {
        font-family: 'Shippori Mincho', serif; font-weight: 800;
        font-size: 1.9rem; letter-spacing: 0.03em; margin: 0 0 6px;
    }
    .gate-sub { font-size: 0.85rem; color: #A8BEE0; margin: 0; }
    .gate-seal {
        position: absolute; top: 28px; right: 34px;
        width: 56px; height: 56px; border-radius: 50%;
        border: 2px solid #C6483A; box-shadow: 0 0 0 3px rgba(198,72,58,0.25) inset;
        display: flex; align-items: center; justify-content: center;
        color: #C6483A; font-family: 'Shippori Mincho', serif; font-weight: 800;
        font-size: 13px; letter-spacing: 0.15em; line-height: 1.1;
        writing-mode: vertical-rl; transform: rotate(-8deg); opacity: 0.9;
    }

    .gate-rows { background: #FBF9F4; padding: 4px 46px 14px; }
    .gate-row {
        display: flex; align-items: baseline; gap: 18px;
        padding: 13px 0; border-bottom: 1px solid #E4DFD0;
        font-size: 0.86rem; color: #3C4A63;
    }
    .gate-row:last-child { border-bottom: none; }
    .gate-row .k {
        font-family: 'Shippori Mincho', serif; font-weight: 700;
        color: #1B2F5E; font-size: 0.82rem; flex: 0 0 42px;
    }

    .stTextInput label p {
        font-family: 'JetBrains Mono', monospace !important; font-size: 11px !important;
        letter-spacing: 0.1em; color: #8A7E63 !important; text-transform: uppercase;
    }
    .stButton button {
        background: #1B2F5E; color: #fff; border: none; border-radius: 8px;
        font-family: 'Noto Sans JP', sans-serif; font-weight: 700; padding: 10px 0;
    }
    .stButton button:hover { background: #12234A; color: #fff; }
    .gate-hint {
        font-family: 'Noto Sans JP', sans-serif; font-size: 0.75rem;
        color: #8A96AC; margin-top: 10px;
    }
    </style>
    <div class="gate-card">
      <div class="gate-band">
        <div class="gate-seal">認証済</div>
        <div class="gate-eyebrow">TOUKI-READER / v1.7</div>
        <div class="gate-title">📋 登記簿 PDF パーサー</div>
        <p class="gate-sub">不動産登記簿PDFを自動解析・所有者チェック</p>
      </div>
      <div class="gate-rows">
        <div class="gate-row"><span class="k">甲区</span><span>所有者・持分を自動抽出</span></div>
        <div class="gate-row"><span class="k">乙区</span><span>抵当権・担保リスクを自動検知</span></div>
        <div class="gate-row"><span class="k">出力</span><span>CSVで一括ダウンロード</span></div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    pw = st.text_input("アクセスパスワード", type="password", placeholder="パスワードを入力")
    if st.button("入る", use_container_width=True):
        if pw == st.secrets["APP_PASSWORD"]:
            st.session_state.authenticated = True
            st.session_state.save_token = True  # 次レンダリングでlocalStorageへ保存
            st.rerun()
        else:
            st.error("パスワードが違います")
    st.markdown('<p class="gate-hint">※初回のみ入力すれば、以降はこの端末で自動的にログインされます。</p>', unsafe_allow_html=True)
    st.stop()
# ───────────────────────────────────────────────────────────

st.markdown("""
<style>
.stApp { background-color: #F4F5F8; }
.block-container { padding-top: 4rem !important; padding-bottom: 2rem !important; }
/* Streamlit上部バーを透明化して背景に馴染ませる */
header[data-testid="stHeader"] {
    background-color: #F4F5F8 !important;
    border-bottom: none !important;
}
.section-hdr {
    background: #1B2F5E; color: white;
    padding: 8px 16px; border-radius: 6px;
    margin: 14px 0 6px; font-weight: bold; font-size: 15px;
}
.owner-full {
    background: #F0FFF4; border: 2px solid #2B6B2B;
    border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;
}
.owner-partial {
    background: #FFF8F0; border: 2px solid #C0580A;
    border-radius: 6px; padding: 10px 14px; margin-bottom: 8px;
}
.risk-red   { color: #C0392B; font-weight: bold; }
.risk-orange{ color: #E07000; font-weight: bold; }
.badge-green  { background:#2B8A6B; color:#fff; border-radius:3px; padding:2px 7px; font-size:13px; }
.badge-gray   { background:#8A9ABB; color:#fff; border-radius:3px; padding:2px 7px; font-size:13px; }
.badge-orange { background:#E07000; color:#fff; border-radius:3px; padding:2px 7px; font-size:13px; }
</style>
""", unsafe_allow_html=True)


# ─── CSV生成（Flask版 export_csv と同じロジック） ────────────────────────────
def make_csv_bytes(record: dict, doc_type: str, history: dict, pdf_name: str) -> bytes:
    COLUMNS = [
        "物件フォルダ", "PDFファイル名", "不動産番号", "種別", "地目種類",
        "所在", "地番_家屋番号", "地積_床面積",
        "区分", "順位", "登記の目的", "受付年月日", "受付番号",
        "所有者_関係者名", "住所", "持分", "持分数値",
        "状態", "名称変更後", "元の氏名",
        "債権額", "債務者", "共担目録番号",
        "リスクフラグ", "確認済", "備考",
    ]
    RISK_YEAR = 1965
    _HOUJIN_RE = re.compile(
        r"株式会社|有限会社|合同会社|合資会社|合名会社"
        r"|一般財団|公益財団|一般社団|公益社団"
        r"|学校法人|社会福祉法人|医療法人|宗教法人"
        r"|独立行政法人|地方公共団体|国$|県$|市$|町$|村$"
        r"|銀行|信用金庫|農業協同組合|農協|漁業協同"
    )

    def _is_houjin(name):
        return bool(_HOUJIN_RE.search(name))

    def _parse_year(date_str):
        if not date_str:
            return None
        m = re.search(r"(19|20)(\d{2})", date_str)
        if m:
            return int(m.group(0))
        for era, base in [("明治", 1867), ("大正", 1911), ("昭和", 1925), ("平成", 1988), ("令和", 2018)]:
            m2 = re.search(era + r"([0-9]+)", date_str)
            if m2:
                return base + int(m2.group(1))
        return None

    def _risk_flag(name, date_str, status):
        if status in ("抹消済み", "移転前", "変更", "参考"):
            return ""
        if _is_houjin(name):
            return ""
        year = _parse_year(date_str)
        if year is None:
            return "登記日不明"
        if year < RISK_YEAR:
            return "相続未登記リスク"
        if datetime.now().year - year >= 30:
            return "要確認（長期未変動）"
        return ""

    def _parse_mochi(name_raw):
        m = re.search(r"[(（]([^)）]*\d+分の\d+[^)）]*)[)）]", name_raw)
        if m:
            frac_str = m.group(1)
            clean = re.sub(r"[(（][^)）]*[)）]", "", name_raw).strip()
            fm = re.search(r"(\d+)分の(\d+)", frac_str)
            f_val = round(int(fm.group(2)) / int(fm.group(1)), 6) if fm else ""
            return clean, frac_str, f_val
        m2 = re.search(r"持分\s*(\d+分の\d+)", name_raw)
        if m2:
            frac_str = m2.group(1)
            clean = re.sub(r"持分\s*\d+分の\d+", "", name_raw).strip()
            fm = re.search(r"(\d+)分の(\d+)", frac_str)
            f_val = round(int(fm.group(2)) / int(fm.group(1)), 6) if fm else ""
            return clean or name_raw.strip(), frac_str, f_val
        return name_raw.strip(), "", ""

    base_rec = {
        "物件フォルダ": "",
        "PDFファイル名": pdf_name,
        "不動産番号": record.get("不動産番号", ""),
        "種別": "土地" if doc_type == "tochi" else "建物",
        "地目種類": record.get("地目", "") or record.get("種類", ""),
        "所在": record.get("所在", ""),
        "地番_家屋番号": record.get("地番", "") or record.get("家屋番号", ""),
        "地積_床面積": record.get("地積_m2", "") or record.get("床面積_m2", ""),
        "確認済": "",
        "備考": "",
    }

    rows = []
    for b in history.get("kouku", []):
        owners_raw = b.get("所有者氏名", "")
        addrs_raw = b.get("所有者住所", "")
        status = b.get("状態", "")
        toroku_dt = b.get("取得日", "") or b.get("受付年月日", "")
        owner_list = [o for o in owners_raw.split(SEP) if o.strip()] or [""]
        addr_list = [a.strip() for a in addrs_raw.split(SEP)]
        for i, owner_raw in enumerate(owner_list):
            name, mochi_str, mochi_f = _parse_mochi(owner_raw)
            addr = addr_list[i] if i < len(addr_list) else ""
            rows.append({**base_rec,
                "区分": "甲区", "順位": b.get("順位", ""),
                "登記の目的": b.get("登記の目的", ""), "受付年月日": b.get("受付年月日", ""),
                "受付番号": b.get("受付番号", ""), "所有者_関係者名": name,
                "住所": addr, "持分": mochi_str, "持分数値": mochi_f,
                "状態": status, "名称変更後": b.get("名称変更後", ""), "元の氏名": b.get("元の氏名", ""),
                "債権額": "", "債務者": "", "共担目録番号": "",
                "リスクフラグ": _risk_flag(name, toroku_dt, status),
            })

    for e in history.get("otsuku", []):
        rows.append({**base_rec,
            "区分": "乙区", "順位": e.get("順位", ""),
            "登記の目的": e.get("登記の目的", ""), "受付年月日": e.get("受付日", ""),
            "受付番号": e.get("受付番号", ""), "所有者_関係者名": e.get("抵当権者", ""),
            "住所": "", "持分": "", "持分数値": "", "状態": e.get("状態", ""),
            "名称変更後": "", "元の氏名": "",
            "債権額": e.get("債権額", ""), "債務者": e.get("債務者", ""),
            "共担目録番号": e.get("共担目録番号", ""), "リスクフラグ": "",
        })

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ─── PDF解析（単ファイル） ───────────────────────────────────────────────────
def analyze_pdf(pdf_bytes: bytes, filename: str) -> dict | None:
    suffix = Path(filename).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        fhash = file_md5(tmp_path)
        raw_text = extract_text(tmp_path)
        doc_type = detect_type(filename, raw_text)
        agent = TochiAgent() if doc_type == "tochi" else TatemonoAgent()
        result = agent.run(tmp_path, fhash)
        if result is None:
            return None
        result["pdf_name"] = filename
        result["doc_type"] = doc_type
        result.pop("pdf_path", None)
        return result
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


# ─── ZIPからPDFを展開して解析 ────────────────────────────────────────────────
def analyze_zip(zip_file) -> list[tuple[str, dict | None]]:
    """ZIPを展開し、PDF一覧を (ファイル名, 解析結果) で返す"""
    results = []
    with zipfile.ZipFile(io.BytesIO(zip_file.getvalue())) as zf:
        pdf_names = sorted([
            n for n in zf.namelist()
            if n.lower().endswith(".pdf") and not n.startswith("__MACOSX")
        ])
        for name in pdf_names:
            pdf_bytes = zf.read(name)
            base_name = Path(name).name  # フォルダパスを除いたファイル名
            result = analyze_pdf(pdf_bytes, base_name)
            results.append((base_name, result))
    return results


# ─── 一括CSVをまとめて生成 ────────────────────────────────────────────────────
def make_bulk_csv(all_results: list[dict]) -> bytes:
    """複数解析結果をまとめて1つのCSVに"""
    buf = io.StringIO()
    COLUMNS = [
        "物件フォルダ", "PDFファイル名", "不動産番号", "種別", "地目種類",
        "所在", "地番_家屋番号", "地積_床面積",
        "区分", "順位", "登記の目的", "受付年月日", "受付番号",
        "所有者_関係者名", "住所", "持分", "持分数値",
        "状態", "名称変更後", "元の氏名",
        "債権額", "債務者", "共担目録番号",
        "リスクフラグ", "確認済", "備考",
    ]
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in all_results:
        single = make_csv_bytes(r["record"], r["doc_type"], r["history"], r["pdf_name"])
        # ヘッダ行を除いて追記
        rows_only = single.decode("utf-8-sig").split("\r\n", 1)
        if len(rows_only) > 1 and rows_only[1].strip():
            buf.write(rows_only[1])
    return buf.getvalue().encode("utf-8-sig")


# ─── 持分タイムライン ────────────────────────────────────────────────────────
def render_timeline(kouku_hist: list):
    import math

    def bare_name(s):
        return re.sub(r'[(（][^)）]*[)）]', '', s).strip()

    def parse_frac(s):
        m = re.search(r'[(（]([0-9]+)分の([0-9]+)[)）]', s)
        return (int(m.group(2)), int(m.group(1))) if m else None

    def gcd(a, b):
        while b:
            a, b = b, a % b
        return a

    def lcm(a, b):
        return a // gcd(a, b) * b

    def short_date(s):
        if not s:
            return ""
        era = {"明治": "M", "大正": "T", "昭和": "S", "平成": "H", "令和": "R"}
        m = re.match(r'(明治|大正|昭和|平成|令和)([0-9]+)年([0-9]+)月([0-9]+)日', s)
        return f"{era.get(m.group(1), m.group(1))}{m.group(2)}.{m.group(3)}.{m.group(4)}" if m else s[:10]

    own_entries = [b for b in kouku_hist if b.get("状態") in ("現在", "移転前") and b.get("所有者氏名")]
    if len(own_entries) < 2:
        return

    # 全所有者収集
    all_persons = []
    for e in own_entries:
        cum = e.get("_cumulative") or {}
        names = list(cum.keys()) if cum else [bare_name(n) for n in (e.get("所有者氏名") or "").split(SEP) if n.strip()]
        for n in names:
            if n and n not in all_persons:
                all_persons.append(n)

    if not all_persons:
        return

    # 共通分母
    g_denom = 1
    for e in own_entries:
        cum = e.get("_cumulative") or {}
        for n, d in cum.values():
            g_denom = lcm(g_denom, d)
        if not cum:
            for o in (e.get("所有者氏名") or "").split(SEP):
                f = parse_frac(o)
                if f:
                    g_denom = lcm(g_denom, f[1])

    # テーブル構築
    rows = []
    for e in own_entries:
        cum = e.get("_cumulative") or {}
        row = {"順位・登記目的": f"{e.get('順位','?')}番 {(e.get('登記の目的') or '')[:12]}\n{short_date(e.get('受付年月日',''))}"}
        total_n = 0
        has_frac = False
        for p in all_persons:
            if cum and p in cum:
                n, d = cum[p]
                norm = n * (g_denom // d)
                total_n += norm
                has_frac = True
                row[p] = f"{g_denom}分の{norm}" if g_denom > 1 else "全部"
            else:
                owner_str = e.get("所有者氏名") or ""
                matched = next((o for o in owner_str.split(SEP) if bare_name(o) == p), None)
                if matched:
                    f = parse_frac(matched)
                    if f:
                        norm = f[0] * (g_denom // f[1])
                        total_n += norm
                        has_frac = True
                        row[p] = f"{g_denom}分の{norm}" if g_denom > 1 else "全部"
                    else:
                        row[p] = "全部"
                else:
                    row[p] = "—"
        row["合計"] = f"{g_denom}分の{total_n}" if has_frac else "—"
        rows.append(row)

    # 現在所有者サマリー行
    cur_e = next((e for e in reversed(own_entries) if e.get("状態") == "現在"), None)
    if cur_e:
        cum = cur_e.get("_cumulative") or {}
        sum_row = {"順位・登記目的": "【現在の所有者】"}
        total_cur = 0
        for p in all_persons:
            if p in cum:
                n, d = cum[p]
                norm = n * (g_denom // d)
                total_cur += norm
                sum_row[p] = f"{g_denom}分の{norm}" if g_denom > 1 else "全部"
            else:
                sum_row[p] = "—"
        sum_row["合計"] = f"{g_denom}分の{total_cur}" if g_denom > 1 else "全部"
        rows.append(sum_row)

    import pandas as pd
    df = pd.DataFrame(rows)
    st.markdown('<div class="section-hdr">📊 持分タイムライン</div>', unsafe_allow_html=True)
    st.dataframe(df, use_container_width=True, hide_index=True)


# ─── 結果表示 ───────────────────────────────────────────────────────────────
def display_result(result: dict):
    record = result.get("record", {})
    doc_type = result.get("doc_type", "tochi")
    history = result.get("history", {})
    pdf_name = result.get("pdf_name", "")

    # 基本情報
    st.markdown('<div class="section-hdr">📄 基本情報</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**種別:** {'土地' if doc_type == 'tochi' else '建物'}")
        st.markdown(f"**不動産番号:** {record.get('不動産番号', '—')}")
        st.markdown(f"**所在:** {record.get('所在', '—')}")
        if doc_type == "tochi":
            st.markdown(f"**地番:** {record.get('地番', '—')}")
            st.markdown(f"**地目:** {record.get('地目', '—')}")
            st.markdown(f"**地積:** {record.get('地積_m2', '—')} ㎡")
        else:
            st.markdown(f"**家屋番号:** {record.get('家屋番号', '—')}")
            st.markdown(f"**種類:** {record.get('種類', '—')}")
            st.markdown(f"**床面積:** {record.get('床面積_m2', '—')} ㎡")
    with col2:
        st.markdown(f"**構造:** {record.get('構造', '—')}")
        st.markdown(f"**抽出日時:** {record.get('抽出日時', '—')}")

    # 所有者情報
    st.markdown('<div class="section-hdr">👤 現在の所有者</div>', unsafe_allow_html=True)
    owners_raw = record.get("所有者氏名", "")
    addrs_raw = record.get("所有者住所", "")
    if owners_raw:
        owner_list = [o.strip() for o in owners_raw.split(SEP) if o.strip()]
        addr_list = [a.strip() for a in addrs_raw.split(SEP)] if addrs_raw else []
        has_partial = any("分の" in o for o in owner_list)
        for i, owner in enumerate(owner_list):
            addr = addr_list[i] if i < len(addr_list) else ""
            card_class = "owner-partial" if has_partial else "owner-full"
            st.markdown(f"""
            <div class="{card_class}">
                <div style="font-weight:bold;font-size:16px;">{owner}</div>
                <div style="color:#555;margin-top:4px;">{addr or '住所不明'}</div>
            </div>""", unsafe_allow_html=True)
    else:
        st.warning("所有者情報を取得できませんでした")

    # 甲区（所有権）履歴
    kouku_hist = history.get("kouku", [])
    if kouku_hist:
        st.markdown('<div class="section-hdr">📋 甲区（所有権）</div>', unsafe_allow_html=True)

        # 差押エントリをグループ化（差押＋対応する差押抹消を同一枠に）
        def kouku_entry_block(b: dict):
            """1エントリ分の内容を描画"""
            status = b.get("状態", "")
            st.markdown(f"**受付日:** {b.get('受付年月日', '—')}")
            if status in ("差押", "差押抹消") or b.get("差押名義人"):
                meigi = b.get("差押名義人", "") or b.get("所有者氏名", "")
                if meigi:
                    st.markdown(f"**差押名義人（執行者）:** {meigi.replace(SEP, ' / ')}")
            else:
                owner_disp = b.get("所有者氏名", "").replace(SEP, " / ")
                if owner_disp:
                    st.markdown(f"**所有者:** {owner_disp}")
            if b.get("所有者住所"):
                st.markdown(f"**住所:** {b['所有者住所'].replace(SEP, ' / ')}")
            if b.get("元の氏名"):
                st.markdown(f"**元の氏名:** {b['元の氏名']}")

        # 差押抹消が参照する順位番号を抽出
        def ref_rank(mokuteki: str) -> str:
            m = re.match(r'([0-9]+)番', mokuteki or "")
            return m.group(1) if m else ""

        handled = set()
        for idx, b in enumerate(kouku_hist):
            if idx in handled:
                continue
            status = b.get("状態", "")
            rank = str(b.get("順位", ""))
            mokuteki = b.get("登記の目的", "")

            if status == "差押":
                # 対応する差押抹消を探す
                pair = [j for j, bj in enumerate(kouku_hist)
                        if j != idx and bj.get("状態") == "差押抹消"
                        and ref_rank(bj.get("登記の目的", "")) == rank]
                if pair:
                    pair_idx = pair[0]
                    bj = kouku_hist[pair_idx]
                    handled.add(idx)
                    handled.add(pair_idx)
                    label = f"順位{rank}　{mokuteki} → 抹消済み　[差押→抹消]"
                    with st.expander(label, expanded=False):
                        st.markdown("**🔴 差押登記**")
                        kouku_entry_block(b)
                        st.markdown("---")
                        st.markdown(f"**✅ 差押抹消　順位{bj.get('順位','')}**")
                        kouku_entry_block(bj)
                else:
                    handled.add(idx)
                    with st.expander(f"順位{rank}　{mokuteki}　[差押・有効]", expanded=True):
                        kouku_entry_block(b)
            else:
                handled.add(idx)
                label = f"順位{rank}　{mokuteki}　[{status}]"
                with st.expander(label, expanded=(status == "現在")):
                    kouku_entry_block(b)

        # 持分タイムライン（移転が2件以上のとき）
        render_timeline(kouku_hist)

    # 乙区（担保権）履歴 + 共同担保目録
    otsuku_hist = history.get("otsuku", [])
    tanpo_list = history.get("tanpo", [])

    # 共担目録を記号でグループ化
    tanpo_groups: dict = {}
    for t in tanpo_list:
        key = t.get("記号及び番号", "不明")
        tanpo_groups.setdefault(key, []).append(t)

    def extract_no(s):
        """第○号 / ○号 / 数字のみ など複数形式に対応"""
        if not s:
            return ""
        # 第1053号 → 1053
        m = re.search(r'第\s*([0-9][0-9/]*)\s*号', s)
        if m:
            return m.group(1).strip()
        # 1053号 → 1053
        m = re.search(r'([0-9][0-9/]*)\s*号', s)
        if m:
            return m.group(1).strip()
        # 数字のみ
        m = re.search(r'([0-9]+)', s)
        return m.group(1) if m else s.strip()

    def find_tanpo_key(kyotan_no: str) -> str | None:
        """共担目録番号から tanpo_groups のキーを特定（複数形式フォールバックあり）"""
        if not kyotan_no or not tanpo_groups:
            return None
        # 1. 完全一致
        if kyotan_no in tanpo_groups:
            return kyotan_no
        # 2. 数字部分で照合
        digits = extract_no(kyotan_no)
        if digits:
            for k in tanpo_groups:
                if extract_no(k) == digits:
                    return k
        # 3. 部分文字列で照合
        for k in tanpo_groups:
            if kyotan_no in k or k in kyotan_no:
                return k
        return None

    if otsuku_hist:
        st.markdown('<div class="section-hdr">🔒 乙区（担保権）</div>', unsafe_allow_html=True)
        for e in otsuku_hist:
            status = e.get("状態", "")
            mokuteki = e.get("登記の目的", "")
            rank = e.get("順位", "")
            with st.expander(f"順位{rank}　{mokuteki}　[{status}]", expanded=(status != "抹消済み")):
                st.markdown(f"**受付日:** {e.get('受付日', '—')}")
                st.markdown(f"**債権額:** {e.get('債権額', '—')}")
                st.markdown(f"**債務者:** {e.get('債務者', '—')}")
                st.markdown(f"**抵当権者:** {e.get('抵当権者', '—')}")

                # 共担目録番号を常に表示
                kyotan_no = e.get("共担目録番号", "")
                if kyotan_no:
                    st.markdown(f"**共担目録番号:** {kyotan_no}")

                # 共同担保目録（抹消済みを含む全エントリで表示）
                matched_key = find_tanpo_key(kyotan_no)
                # 乙区1件・目録1件のときはキー不問でマッチ
                if matched_key is None and len(tanpo_groups) == 1:
                    matched_key = next(iter(tanpo_groups))

                if matched_key:
                    tanpo_entries = tanpo_groups[matched_key]
                    with st.expander(f"　▸ 共同担保目録　{matched_key}", expanded=False):
                        for t in tanpo_entries:
                            content = t.get("内容", "")
                            if t.get("状態") == "抹消済み":
                                st.markdown(f"~~{content}~~")
                            else:
                                st.markdown(f"- {content}")
    else:
        st.success("乙区なし（担保権・抵当権の記録なし）")

    # CSVダウンロード
    st.markdown("---")
    csv_bytes = make_csv_bytes(record, doc_type, history, pdf_name)
    fname = f"touki_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    st.download_button(
        label="📥 CSVダウンロード",
        data=csv_bytes,
        file_name=fname,
        mime="text/csv",
        use_container_width=True,
    )


# ─── メインUI ───────────────────────────────────────────────────────────────
def main():
    st.markdown(
        '<div style="background:#1B2F5E;padding:6px 16px;border-radius:6px;margin-bottom:4px;">'
        '<span style="color:#fff;font-size:17px;font-weight:bold;">📋 登記簿 PDF パーサー</span>'
        '<span style="color:#A8BEE0;font-size:12px;margin-left:10px;">v1.7 — Streamlit版</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    tab_single, tab_folder = st.tabs(["📄 PDFファイル（単体・複数）", "📁 フォルダ一括（ZIP）"])

    # ── タブ1：PDF直接アップロード ─────────────────────────────────────────
    with tab_single:
        uploaded_files = st.file_uploader(
            "登記簿PDFをアップロード（複数同時対応）",
            type=["pdf"],
            accept_multiple_files=True,
            help="土地・建物の全部事項証明書PDFをアップロードしてください",
            key="pdf_uploader",
        )
        if not uploaded_files:
            st.info("PDFをドロップ、またはクリックして選択してください。複数ファイル同時対応。")
        else:
            all_results = []
            for uploaded_file in uploaded_files:
                st.markdown(f"### 📄 {uploaded_file.name}")
                with st.spinner("解析中..."):
                    result = analyze_pdf(uploaded_file.getvalue(), uploaded_file.name)
                if result is None:
                    st.error("解析に失敗しました。登記簿（全部事項証明書）のPDFか確認してください。")
                    continue
                all_results.append(result)
                display_result(result)
                st.divider()

            # 複数ファイルのとき一括CSVダウンロード
            if len(all_results) >= 2:
                st.markdown("### 📦 一括CSVダウンロード")
                bulk_csv = make_bulk_csv(all_results)
                fname = f"touki_bulk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                st.download_button(
                    label=f"📥 全{len(all_results)}件まとめてCSVダウンロード",
                    data=bulk_csv,
                    file_name=fname,
                    mime="text/csv",
                    use_container_width=True,
                )

    # ── タブ2：フォルダ一括（ZIP） ────────────────────────────────────────
    with tab_folder:
        st.markdown("**方法A：ZIP圧縮してアップロード**")
        st.markdown("""
        1. 登記簿PDFが入ったフォルダを右クリック →「ZIPに圧縮」
        2. 作成されたZIPファイルをアップロード
        3. フォルダ内の全PDFを一括解析してまとめてCSV出力できます
        """)
        zip_file = st.file_uploader(
            "ZIPファイルをアップロード",
            type=["zip"],
            help="PDFが複数入ったフォルダをZIP圧縮してアップロードしてください",
            key="zip_uploader",
        )
        if zip_file:
            with st.spinner("ZIPを展開して解析中..."):
                zip_results = analyze_zip(zip_file)

            ok = [(name, r) for name, r in zip_results if r is not None]
            ng = [name for name, r in zip_results if r is None]

            st.success(f"✅ {len(ok)}件 解析成功　{'　⚠ ' + str(len(ng)) + '件 失敗' if ng else ''}")
            if ng:
                st.warning("解析失敗: " + "、".join(ng))

            if ok:
                # 一括CSVダウンロード
                bulk_csv = make_bulk_csv([r for _, r in ok])
                fname = f"touki_bulk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                st.download_button(
                    label=f"📥 全{len(ok)}件まとめてCSVダウンロード",
                    data=bulk_csv,
                    file_name=fname,
                    mime="text/csv",
                    use_container_width=True,
                )

                # 個別結果を展開表示
                st.markdown("---")
                for name, result in ok:
                    with st.expander(f"📄 {name}", expanded=False):
                        display_result(result)

        st.markdown("---")
        st.markdown("**方法B：フォルダを直接選択（ZIP圧縮不要・Chrome/Edge推奨）**")
        st.caption("クリックするとOSのフォルダ選択ダイアログが開き、選んだフォルダ内のPDFをまとめて読み込みます。Safari等の非対応ブラウザでは通常のファイル選択になるため、その場合は方法Aをご利用ください。")

        folder_files = st.file_uploader(
            "フォルダを直接選択してアップロード",
            type=["pdf"],
            accept_multiple_files=True,
            key="folder_direct_uploader",
        )
        components.html("""
        <script>
        (function enableFolderSelect() {
          try {
            var doc = window.parent.document;
            doc.querySelectorAll('[data-testid="stFileUploader"]').forEach(function(box) {
              if ((box.innerText || '').indexOf('フォルダを直接選択してアップロード') !== -1) {
                var input = box.querySelector('input[type="file"]');
                if (input && !input.hasAttribute('webkitdirectory')) {
                  input.setAttribute('webkitdirectory', '');
                  input.setAttribute('directory', '');
                }
              }
            });
          } catch (e) {}
          setTimeout(enableFolderSelect, 800);
        })();
        </script>
        """, height=0)

        if folder_files:
            with st.spinner(f"{len(folder_files)}件のPDFを解析中..."):
                folder_results = []
                folder_failed = []
                for f in folder_files:
                    r = analyze_pdf(f.getvalue(), f.name)
                    if r:
                        folder_results.append(r)
                    else:
                        folder_failed.append(f.name)

            st.success(f"✅ {len(folder_results)}件 解析成功　{'　⚠ ' + str(len(folder_failed)) + '件 失敗' if folder_failed else ''}")
            if folder_failed:
                st.warning("解析失敗: " + "、".join(folder_failed))

            if folder_results:
                bulk_csv = make_bulk_csv(folder_results)
                fname = f"touki_bulk_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                st.download_button(
                    label=f"📥 全{len(folder_results)}件まとめてCSVダウンロード",
                    data=bulk_csv,
                    file_name=fname,
                    mime="text/csv",
                    use_container_width=True,
                    key="folder_bulk_dl",
                )
                st.markdown("---")
                for r in folder_results:
                    with st.expander(f"📄 {r['pdf_name']}", expanded=False):
                        display_result(r)


if __name__ == "__main__":
    main()