"""
登記簿ビューアー — Streamlit版 v1.7
GitHub + Streamlit Cloud デプロイ対応
"""
import sys
import io
import csv
import re
import tempfile
from pathlib import Path
from datetime import datetime

import streamlit as st

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

st.markdown("""
<style>
.stApp { background-color: #F4F5F8; }
.block-container { padding-top: 1.5rem; }
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


# ─── PDF解析 ────────────────────────────────────────────────────────────────
def analyze_pdf(uploaded_file) -> dict | None:
    suffix = Path(uploaded_file.name).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        tmp_path = Path(tmp.name)
    try:
        fhash = file_md5(tmp_path)
        raw_text = extract_text(tmp_path)
        doc_type = detect_type(uploaded_file.name, raw_text)
        agent = TochiAgent() if doc_type == "tochi" else TatemonoAgent()
        result = agent.run(tmp_path, fhash)
        if result is None:
            return None
        result["pdf_name"] = uploaded_file.name
        result["doc_type"] = doc_type
        result.pop("pdf_path", None)
        return result
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


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
        for b in kouku_hist:
            status = b.get("状態", "")
            mokuteki = b.get("登記の目的", "")
            rank = b.get("順位", "")
            badge = {"現在": "badge-green", "抹消済み": "badge-gray"}.get(status, "badge-orange")
            label = f"順位{rank}　{mokuteki}　[{status}]"
            with st.expander(label, expanded=(status == "現在")):
                st.markdown(f"**受付日:** {b.get('受付年月日', '—')}")
                owner_disp = b.get("所有者氏名", "").replace(SEP, " / ")
                st.markdown(f"**所有者:** {owner_disp}")
                if b.get("所有者住所"):
                    st.markdown(f"**住所:** {b['所有者住所'].replace(SEP, ' / ')}")
                if b.get("元の氏名"):
                    st.markdown(f"**元の氏名:** {b['元の氏名']}")

    # 乙区（担保権）履歴
    otsuku_hist = history.get("otsuku", [])
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
    st.markdown("""
    <div style="background:#1B2F5E;padding:14px 24px;border-radius:8px;margin-bottom:20px;">
        <span style="color:#fff;font-size:22px;font-weight:bold;">📋 登記簿 PDF パーサー</span>
        <span style="color:#A8BEE0;font-size:14px;margin-left:12px;">v1.7 — Streamlit版</span>
    </div>
    """, unsafe_allow_html=True)

    uploaded_files = st.file_uploader(
        "登記簿PDFをアップロード（複数同時対応）",
        type=["pdf"],
        accept_multiple_files=True,
        help="土地・建物の全部事項証明書PDFをアップロードしてください",
    )

    if not uploaded_files:
        st.info("PDFをアップロードすると自動で解析します。複数ファイルを一度にドロップすることも可能です。")
        return

    for uploaded_file in uploaded_files:
        st.markdown(f"### 📄 {uploaded_file.name}")
        with st.spinner(f"解析中..."):
            result = analyze_pdf(uploaded_file)

        if result is None:
            st.error("解析に失敗しました。登記簿（全部事項証明書）のPDFか確認してください。")
            continue

        display_result(result)
        st.divider()


if __name__ == "__main__":
    main()