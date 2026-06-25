"""
不動産登記簿 PDFパーサー v3
- 土地全部事項 / 建物全部事項 を自動判別
- 土地・建物で別CSVに出力
- サブフォルダ再帰処理・差分検出つき
"""
import pdfplumber
import re
import csv
import sqlite3
import hashlib
import unicodedata
from pathlib import Path
from datetime import datetime

# ---- パス設定 ----
import sys as _sys
if getattr(_sys, 'frozen', False):
    # PyInstaller exe: __file__ は _internal/scripts/ 内を指すため exe の親を使う
    BASE_DIR = Path(_sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent.parent

_TOUKI_BASE = BASE_DIR / "登記簿公図データ"
INPUT_DIR   = _TOUKI_BASE          # PDFは登記簿公図データ直下
MAP_DIR     = _TOUKI_BASE / "公図"
CSV_DIR     = BASE_DIR / "output_csv"
DB_PATH     = BASE_DIR / "database" / "touki.db"
REPORT_DIR  = BASE_DIR / "reports"


def setup_dirs():
    """必要なディレクトリを作成する（明示的に呼び出す）"""
    for d in [CSV_DIR, DB_PATH.parent, REPORT_DIR, MAP_DIR]:
        d.mkdir(parents=True, exist_ok=True)

# ---- 全角→半角変換 ----
def zen2han(text: str) -> str:
    if not text:
        return ""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif ch == '\u3000':
            result.append(' ')
        else:
            result.append(ch)
    return "".join(result)

# ---- テキスト抽出 ----
def extract_text(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    raw = "\n".join(pages)
    return unicodedata.normalize('NFKC', raw)

# ---- 定数 ----
GENGOU   = r'(?:明治|大正|昭和|平成|令和)'
DATE_PAT = GENGOU + r'[0-9]+年[0-9]+月[0-9]+日'
SEP = '；'

# ---- セル末尾から日本語名を取り出す ----
_JP_NAME_SKIP = re.compile(
    r'移記|規定|附則|令第|登記の目的|受付年月日|権利者その他'
    r'|順位[0-9]|付記|下線|の登記を|により移記|法務省令'
)
def extract_jp_name(cells: list) -> str:
    for cell in reversed(cells):
        s = re.sub(r'\s+', '', cell).strip()
        if not s:
            continue
        if not re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', s):
            continue
        if _JP_NAME_SKIP.search(s):
            continue
        # \u6301\u5206\u5206\u6570\u306f\u540d\u524d\u3067\u306f\u306a\u3044
        if re.fullmatch(r'\u6301\u5206\s*[0-9][0-9\u5206\u306e/]*', s):
            continue
        # \u756a\u5730\u3092\u542b\u3080\u6587\u5b57\u5217\u306f\u4f4f\u6240\uff082\u884c\u306b\u5206\u304b\u308c\u305f\u4f4f\u6240\u7d9a\u304d\u300c\u756a\u57304\u300d\u306a\u3069\u3082\u9664\u5916\uff09
        if re.search(r'\u756a\u5730', s):
            continue
        # \u4f4f\u6240\u7d9a\u304d\u884c: \u300c\u3001C\u68df-13\u53f7\u300d\u300c\u30fbB\u68df2\u53f7\u5ba4\u300d\u306a\u3069\u8aad\u70b9\u30fb\u4e2d\u70b9\u3067\u59cb\u307e\u308b\u884c
        if s.startswith('\u3001') or s.startswith('\u30fb'):
            continue
        # \u68df\u30fb\u53f7\u756a\u53f7\uff08\u30d3\u30eb\u53f7\u5ba4\uff09: \u300cC\u68df-13\u53f7\u300d\u300c\u7b2c2\u68df\u300d\u306a\u3069
        if re.search(r'[A-Za-z\u7b2c][\u68df\u53f7]|[0-9]+[\u68df\u53f7](?:\u5ba4)?$', s):
            continue
        # \u300c\u756a\u5730XX\u300d\u306e\u6539\u884c\u7d9a\u304d: \u300c\u573058\u300d\u300c\u5730\u306e2\u300d\u306a\u3069\uff08\u756a\u304c\u524d\u884c\u672b\u5c3e\u306b\u3042\u308b\u5834\u5408\uff09
        if re.match(r'^\u5730[0-9\u306e]', s):
            continue
        # \u30d3\u30eb\u30fb\u30d5\u30ed\u30a2\u756a\u53f7\uff08\u4f4f\u6240\u7d9a\u304d\uff09: \u300cA\u30d3\u30eb1\u968e\u300d\u300c\u7b2c3\u30d3\u30eb\u300d\u306a\u3069
        if re.search(r'\u30d3\u30eb[0-9A-Za-z]|[0-9]+\u968e$', s):
            continue
        return s
    return ""

# ====================================================
# 種別判定
# ====================================================
def detect_type(filename: str, text: str) -> str:
    """'tochi' / 'tatemono' / 'other' を返す"""
    name = filename
    # 法人登記簿はスキップ（不動産登記ではない）
    if '法人登記簿' in name:
        return 'other'
    if '土地全部事項' in name or '土地全部' in name:
        return 'tochi'
    if '建物全部事項' in name or '建物全部' in name:
        return 'tatemono'
    # ファイル名で判断できない場合はテキストで判断
    t = zen2han(text)
    if re.search(r'表\s*題\s*部.*?土地の表示', t):
        return 'tochi'
    if re.search(r'表\s*題\s*部.*?建物の表示', t):
        return 'tatemono'
    return 'other'

# ====================================================
# 共通: 不動産番号抽出
# ====================================================
def parse_fudosan_no(t: str) -> str:
    m = re.search(r'不動産番号[│┃\s]*([0-9]{13})', t)
    return m.group(1) if m else ""

# ====================================================
# 共通: セクション分割
# ====================================================
def split_sections(t: str) -> dict:
    hyodai_end = re.search(r'権\s*利\s*部.*?甲\s*区', t)
    kouku_end  = re.search(r'権\s*利\s*部.*?乙\s*区', t)

    # 共同担保目録開始位置：PDFレイアウト由来のスペース込みパターンで検索
    # 列見出し「担保の目的である権利の表示」より前にある「共同担保目録」見出しを起点とする
    _TANPO_HDR = r'共\s*同\s*担\s*保\s*目\s*録'
    _COL_HDR   = r'担\s*保\s*の\s*目\s*的\s*で\s*あ\s*る\s*権\s*利\s*の\s*表\s*示'
    tanpo_pos: int | None = None
    col_hdr = re.search(_COL_HDR, t)
    if col_hdr:
        # 列見出しより前の最後の「共同担保目録」見出しを使う
        prev_matches = list(re.finditer(_TANPO_HDR, t[:col_hdr.start()]))
        tanpo_pos = prev_matches[-1].start() if prev_matches else col_hdr.start()
    else:
        matches = list(re.finditer(_TANPO_HDR, t))
        # 乙区エントリ内のインライン参照「共同担保 目録第XXXX号」は除外し
        # 実際のセクション見出し（直後に第X号が来ないもの）のみを使う
        _INLINE_REF = re.compile(r'第[0-9]')
        section_matches = [
            m for m in matches
            if not _INLINE_REF.search(t[m.end():m.end() + 30])
        ]
        if section_matches:
            tanpo_pos = section_matches[-1].start()
        elif len(matches) == 1 and (kouku_end and matches[0].start() > kouku_end.end() + 200):
            tanpo_pos = matches[0].start()

    hyodai    = t[:hyodai_end.start()] if hyodai_end else t
    kouku_end_pos = kouku_end.start() if kouku_end else (tanpo_pos if tanpo_pos is not None else len(t))
    kouku     = t[hyodai_end.end():kouku_end_pos] if hyodai_end else ""
    otsuku_end_pos = tanpo_pos if tanpo_pos is not None else len(t)
    otsuku    = t[kouku_end.end():otsuku_end_pos] if kouku_end else ""
    tanpo     = t[tanpo_pos:] if tanpo_pos is not None else ""

    return {"hyodai": hyodai, "kouku": kouku, "otsuku": otsuku, "tanpo": tanpo}

# ====================================================
# 表題部パース：土地
# ====================================================
def parse_hyodai_tochi(t: str) -> dict:
    data = {"所在": "", "地番": "", "地目": "", "地積_m2": ""}

    # Primary: ┃所 在│ アンカーで直接取得（旧住所（浜北市→浜松市新原 等）にも対応）
    soi_matches = re.findall(r'┃所\s*在[│┃]\s*([^│┃\n]+)', t)
    soi_candidates = [s.strip() for s in soi_matches
                      if not re.search(r'変更|登記|移記|調製', s)]
    if soi_candidates:
        data["所在"] = soi_candidates[-1].strip()
    else:
        # Fallback: 市郡+町丁目字 パターン
        candidates = re.findall(
            r'[┃│]\s*((?:[^\s│┃]{1,10}(?:市|郡))\S+?(?:町|丁目|字)\S*?)\s*[│┃\n]', t
        )
        candidates = [c for c in candidates if not re.search(r'変更|登記|移記|調製', c)]
        if candidates:
            data["所在"] = candidates[-1].strip()

    matches = re.findall(r'[┃│]\s*([0-9]+番[0-9]*)\s*[│┃]', t)
    if matches:
        data["地番"] = matches[-1].strip()

    地目候補 = re.findall(
        r'[┃│]\s*(宅地|田|畑|山林|原野|雑種地|公衆用道路|用悪水路|ため池|墓地)\s*[│┃]', t
    )
    if 地目候補:
        data["地目"] = 地目候補[-1].strip()

    matches = re.findall(r'[┃│]\s*([0-9]+[:.][0-9]{0,2})\s*[│┃]', t)
    valid = [m for m in matches if not re.match(r'^0+[:.]', m)]
    if valid:
        raw = valid[-1].replace(':', '.')
        data["地積_m2"] = raw + '00' if raw.endswith('.') else raw

    return data

# ====================================================
# 表題部パース：建物
# ====================================================
def parse_hyodai_tatemono(t: str) -> dict:
    data = {"所在": "", "家屋番号": "", "種類": "", "構造": "", "床面積_m2": ""}

    # 所在（┃所 在│ アンカーで取得 — スペース入りの番地にも対応）
    soi_matches = re.findall(r'┃所\s*在[│┃]\s*([^│┃\n]+)', t)
    soi_candidates = [s.strip() for s in soi_matches
                      if not re.search(r'変更|登記|移記|調製', s)]
    if soi_candidates:
        data["所在"] = soi_candidates[-1].strip()

    # 家屋番号（「家屋番号│〇〇番〇〇」）
    m = re.search(r'家屋番号\s*[│┃\s]*([^\s│┃\n]+)', t)
    if m:
        data["家屋番号"] = m.group(1).strip()

    # 種類（居宅・店舗・事務所・倉庫・工場など）
    種類候補 = re.findall(
        r'[┃│]\s*(居宅|店舗|事務所|倉庫|工場|共同住宅|寄宿舎|旅館|ホテル|診療所|老人ホーム'
        r'|保育所|幼稚園|学校|図書館|体育館|公会堂|劇場|映画館|遊技場'
        r'|ガレージ|車庫|物置|附属建物)\s*[│┃]', t
    )
    if 種類候補:
        # 重複除去して結合
        seen = []
        for s in 種類候補:
            if s not in seen:
                seen.append(s)
        data["種類"] = SEP.join(seen)

    # 構造（「木造〇〇葺〇階建」など — 2行に分割されていても対応）
    sm = re.search(
        r'[┃│]\s*((?:木造|鉄骨造|鉄筋コンクリート造|鉄骨鉄筋コンクリート造|'
        r'コンクリートブロック造|れんが造|石造|土蔵造|軽量鉄骨造)'
        r'[^│┃\n]*)',
        t)
    if sm:
        part1 = sm.group(1).strip()
        if '建' in part1:
            data["構造"] = re.sub(r'\s+', '', part1)
        else:
            # 構造が2行に分割されている場合、次のセルに「X階建」を探す
            tail = re.search(r'[│┃]\s*([^│┃\n]*(?:[0-9]+階)?建[^│┃\n]*)',
                             t[sm.end():sm.end() + 200])
            if tail:
                data["構造"] = re.sub(r'\s+', '', part1 + tail.group(1))
            else:
                data["構造"] = re.sub(r'\s+', '', part1)

    # 床面積（各階を個別表示）
    floor_area_pairs = re.findall(r'([0-9]+)階\s+([0-9]+[:.][0-9]{0,2})', t)
    if floor_area_pairs:
        parts = [f"{f}階:{a.replace(':', '.').rstrip('.')}㎡" for f, a in floor_area_pairs]
        data["床面積_m2"] = " / ".join(parts)
    else:
        # 単一階または階数不明の場合
        matches = re.findall(r'[┃│]\s*([0-9]{2,}[:.][0-9]{2})\s*[│┃]', t)
        if matches:
            data["床面積_m2"] = matches[-1].replace(':', '.') + "㎡"

    return data

# ====================================================
# 甲区パース（土地・建物共通）
# ====================================================
def parse_kouku(kouku: str) -> dict:
    data = {
        "所有者氏名": "",
        "所有者住所": "",
        "取得原因":   "",
        "取得日":     "",
        "受付年月日": "",
        "受付番号":   "",
    }

    lines = kouku.split('\n')

    # 取得原因・取得日
    # 日付と原因語の間に「担保不動産」等の修飾が入る場合があるため {0,20}? を使用
    _CAUSE_PAT = (r'売買|相続|贈与|競売|公売|交換|合併|信託|財産分与|'
                  r'収用|判決|時効|遺産分割|換地処分|真正な登記名義の回復')
    cause_matches = re.findall(
        r'原因\s+(' + DATE_PAT + r')[^\n│┃]{0,20}?(' + _CAUSE_PAT + r')',
        kouku,
        re.MULTILINE,
    )
    if cause_matches:
        data["取得日"]   = cause_matches[-1][0]
        data["取得原因"] = cause_matches[-1][1]

    # 順位番号行を全て検出（付記を除く）
    rank_positions = [
        i for i, line in enumerate(lines)
        if re.search(r'┃\s*[0-9]+\s*│', line) and '付記' not in line
    ]
    # 所有者/共有者を持つ最後の順位ブロックを使う
    last_block_start = 0
    for rp in reversed(rank_positions):
        next_rps = [p for p in rank_positions if p > rp]
        block_end = next_rps[0] if next_rps else len(lines)
        block = lines[rp:block_end]
        if any('所有者' in ln or '共有者' in ln or '受託者' in ln for ln in block):
            last_block_start = rp
            break
    last_block = lines[last_block_start:]

    # last_block 内で付記が始まる位置を特定
    fuki_start_in_block = None
    for i, ln in enumerate(last_block):
        if re.search(r'┃\s*付記', ln):
            fuki_start_in_block = i
            break
    main_block = last_block[:fuki_start_in_block] if fuki_start_in_block is not None else last_block
    fuki_block  = last_block[fuki_start_in_block:] if fuki_start_in_block is not None else []

    # 受付年月日・受付番号: 付記を除く所有権移転エントリから取得
    main_text = '\n'.join(main_block)
    receipt_matches = re.findall(
        r'(' + DATE_PAT + r')[^\n]*\n[^\n]*第([0-9]+)号',
        main_text
    )
    if receipt_matches:
        data["受付年月日"] = receipt_matches[-1][0]
        data["受付番号"]   = "第" + receipt_matches[-1][1] + "号"

    names, addrs = [], []

    # 共有者モード判定
    has_kyoyusha = any('共有者' in ln for ln in last_block)

    if has_kyoyusha:
        # ---- 共有者（複数所有）パース ----
        SKIP_RE = re.compile(
            r'移記|規定|附則|令第|登記の目的|受付年月日|権利者その他|順位[0-9]|付記|目 的|年月日|番 号|^[*＊]|下線'
        )
        _ADDR_RE = re.compile(r'番地|丁目|[0-9]+番[0-9]+号')
        cur_addr, cur_持分, prev_was_addr = '', '', False

        for line in last_block:
            parts = re.split(r'[│┃]', line)
            # 4列目（権利者欄）＝最後の非空セル
            cell = next((p.strip() for p in reversed(parts) if p.strip()), '')
            if not cell or SKIP_RE.search(cell):
                continue

            # 「共有者」キーワード（住所が同行に続く場合も考慮）
            if '共有者' in cell:
                addr_part = re.sub(r'共有者\s*', '', cell).strip()
                if re.search(r'番地|丁目|[0-9]+番[0-9]+号', addr_part):
                    cur_addr = addr_part
                cur_持分 = ''
                prev_was_addr = False
                continue

            # 住所行: 番地・丁目・○番○号 を含む
            if re.search(r'番地|丁目|[0-9]+番[0-9]+号', cell):
                cur_addr = cell
                cur_持分 = ''
                prev_was_addr = True
                continue

            # 持分行: X分のY
            if re.search(r'[0-9]+分の[0-9]+', cell):
                m = re.search(r'([0-9]+分の[0-9]+)', cell)
                if m:
                    cur_持分 = m.group(1)
                prev_was_addr = False
                continue

            # 氏名行: 日本語のみ（ASCII数字なし・住所語句なし）
            clean = re.sub(r'\s+', '', cell)
            if (re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', clean)
                    and not re.search(r'[0-9]', clean)
                    and not re.search(r'番地|丁目|登記|移転|移記|換地|相続|売買|贈与|原因', clean)):
                if prev_was_addr:
                    cur_addr += clean
                    prev_was_addr = False
                    continue
                entry = f"{clean}({cur_持分})" if cur_持分 else clean
                names.append(entry)
                addrs.append(cur_addr)
                cur_持分 = ''
                prev_was_addr = False
    else:
        # ---- 所有者 / 受託者（単独）パース ----
        for i, line in enumerate(last_block):
            if '所有者' not in line and '受託者' not in line:
                continue
            addr_m = re.search(r'(?:所有者|受託者)\s+([^│┃\n]+?)(?:\s*[│┃]|$)', line)
            if not addr_m:
                continue
            addr = addr_m.group(1).strip()

            持分 = ""
            持分_m = re.search(r'持分([0-9]+分の[0-9]+)', line)
            if not 持分_m and i + 2 < len(last_block):
                持分_m = re.search(r'持分([0-9]+分の[0-9]+)', last_block[i + 2])
            if 持分_m:
                持分 = 持分_m.group(1)

            name = ""
            for offset in [1, 2]:
                if i + offset < len(last_block):
                    cells = re.split(r'[│┃]', last_block[i + offset])
                    name = extract_jp_name(cells)
                    if name:
                        break

            # フォールバック：「所有者　浜松市」のように名前が同行にある場合
            if not name and addr:
                addr_clean = re.sub(r'\s+', '', addr)
                if (re.search(r'[一-鿿]', addr_clean)
                        and not re.search(r'[0-9]', addr_clean)
                        and not re.search(r'番地|丁目|番[0-9]|号室', addr_clean)):
                    name = addr_clean
                    addr = ""

            names.append(f"{name}({持分})" if (name and 持分) else (name or ""))
            addrs.append(addr)

    if names:
        data["所有者氏名"] = SEP.join(names)
        data["所有者住所"] = SEP.join(addrs)

    # 付記エントリで住所変更・住所移転が登記されている場合、所有者住所を更新
    if fuki_block and data["所有者住所"]:
        in_addr_change = False
        for ln in fuki_block:
            if re.search(r'付記', ln) and re.search(r'住所変更|住所移転', ln):
                in_addr_change = True
            if in_addr_change:
                am = re.search(r'住所\s+([^\n│┃]{4,}?)(?:\s*[│┃]|$)', ln)
                if am:
                    new_addr = re.sub(r'\s+', '', am.group(1)).strip()
                    if new_addr:
                        addr_list = data["所有者住所"].split(SEP)
                        addr_list[-1] = new_addr
                        data["所有者住所"] = SEP.join(addr_list)
                        in_addr_change = False

    # 取得日が空（所有権保存のみ等）の場合、受付年月日を代用
    if not data["取得日"] and data["受付年月日"]:
        data["取得日"] = data["受付年月日"]

    return data

# ====================================================
# 乙区パース ヘルパー
# ====================================================
def _find_teito_name(lines: list, start: int) -> str:
    """
    start 行以降から抵当権者/根抵当権者の名称を抽出する。
    住所行・外国語断片行をスキップし、最後に現れる日本語名を返す。
    複数行にわたる住所（ページまたぎも含む）に対応。
    """
    NEW_ENTRY = re.compile(
        r'[┃│]\s*(?:[0-9]+|付記)\s*[│┃].*?(?:根?抵当権|登記の目的|移転|変更|抹消)'
    )
    STOP      = re.compile(r'共同担保|担保目録|┠─|┗━|┏━')
    # 住所の断片として無視するパターン
    ADDR      = re.compile(r'[0-9]+番地|丁目|[0-9]+番[0-9]|[0-9]+号\s*[┃│]|[0-9][A-Z]{2,}|^[A-Z0-9]{3,}')
    SKIP      = re.compile(r'気付|営業所|取扱店|日本における|支店\s*$')

    candidates = []
    for j in range(start, min(start + 18, len(lines))):
        line = lines[j]
        if STOP.search(line) or NEW_ENTRY.search(line):
            break
        cells = re.split(r'[│┃]', line)
        cell  = next((c.strip() for c in reversed(cells) if c.strip()), '')
        clean = re.sub(r'\s+', '', cell)
        if not clean:
            continue
        if ADDR.search(clean):
            continue
        if SKIP.search(clean):
            continue
        if re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', clean):
            candidates.append(clean)

    return candidates[-1] if candidates else ""


def _find_debtor_name(lines: list, start: int) -> str:
    """債務者の氏名を start 行以降から抽出する（住所の次の行）"""
    STOP = re.compile(r'根?抵当権者|共同担保|┠─|┗━')
    for j in range(start, min(start + 4, len(lines))):
        line = lines[j]
        if STOP.search(line):
            break
        cells = re.split(r'[│┃]', line)
        cell  = next((c.strip() for c in reversed(cells) if c.strip()), '')
        clean = re.sub(r'\s+', '', cell)
        # 住所行（番地・丁目）はスキップ
        if re.search(r'[0-9]+番地|[0-9]+丁目|[0-9]+番[0-9]', clean):
            continue
        if re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', clean) and clean:
            return clean
    return ""


# ====================================================
# 乙区：権利種別パターン定数（司法書士監修・全種別対応）
# ====================================================

# 乙区 新規エントリ行を検出する正規表現（根抵当権設定以外の権利も網羅）
# ※ zen2han後は全角括弧（）→ 半角()に変換されるため \( \) で記述
_OTSUKU_ENTRY_RE = re.compile(
    r'[┃│]\s*([0-9]+(?:\([あ-んア-ン]\))?)\s*[│┃].*?'
    r'(?:根?抵当権(?:設定|移転|一部移転|変更)'
    r'|差押え?(?:登記)?|仮差押え?(?:登記)?|競売による差押え?'
    r'|地上権設定|区分地上権設定|地役権設定|永小作権設定|採石権設定'
    r'|賃借権設定|使用借権設定|配偶者居住権設定|質権設定'
    r'|根?抵当権設定仮登記)'
)

# 乙区 付記エントリ検出パターン（根抵当権移転・一部移転・元本確定など）
_OTSUKU_FUKI_RE = re.compile(
    r'[┃│]\s*([0-9]+)\s*付記\s*([0-9]+)\s*号?\s*[│┃]'
)

# 乙区 抹消・解除エントリの順位番号検出パターン（文字列、re.findall用）
# ※ zen2han後に(あ)(い)がサフィックスとして付く場合も許容
_SUFF = r'(?:\([あ-んア-ン]\))?'   # オプションの（あ）（い）サフィックス
_OTSUKU_CANCEL_PAT = (
    r'([0-9]+' + _SUFF + r'\s*番' + _SUFF +
    r'(?:(?:、|,)\s*[0-9]+' + _SUFF + r'\s*番' + _SUFF + r')*)'
    r'(?:根?抵当権|地上権|賃借権|差押え?|仮差押え?'
    r'|配偶者居住権|質権|地役権|区分地上権|永小作権)'
    r'(?:抹消|解除)'
)

# 甲区 所有権系登記の目的パターン（現在/移転前 の判定に使用）
# ※ 参考（仮登記・差押・更正等）はここに含めない
_KOUKU_OWNERSHIP_RE = re.compile(
    r'所有権(?:一部)?(?:移転|保存|登記)'
    r'|持分(?:全部|一部)移転'
    r'|共有者全員持分全部移転'
    r'|[^\n│┃]*を除く(?:共有者全員持分全部移転|所有権)'  # 例: 戸塚幸宏を除く共有者全員持分全部移転
    r'|合併(?:による)?(?:所有権|持分)'                   # 合併による所有権登記・持分移転
    r'|(?:会社?)?更生(?:計画|法|手続)?(?:による)?(?:所有権|持分)'  # 会社更生法・更生計画による所有権移転
    r'|仮登記に基づく本登記'
    r'|換地処分'  # 土地改良法換地処分による所有権登記
)

# ====================================================
# 乙区パース（土地・建物共通）
# ====================================================
def parse_otsuku(otsuku: str) -> dict:
    data = {
        "抵当権件数": "0",
        "抵当権債権額": "",
        "抵当権債務者": "",
        "抵当権者":    "",
    }
    if not otsuku.strip():
        return data

    lines = otsuku.split('\n')
    entries, current = [], {}
    cancelled_ranks = set()

    # 複数行にまたがる抹消エントリも確実に検出するためフラット化して検索
    _cancel_flat = re.sub(r'\s*\n\s*', ' ', otsuku)
    for cancel_entry in re.findall(_OTSUKU_CANCEL_PAT, _cancel_flat):
        for num in re.findall(r'([0-9]+)\s*番', cancel_entry):
            cancelled_ranks.add(num)

    for i, line in enumerate(lines):

        rank_m = _OTSUKU_ENTRY_RE.search(line)
        if rank_m:
            if current:
                entries.append(current)
            current = {"順位": rank_m.group(1)}

        if not current:
            continue

        # 受付日
        date_m = re.search(r'(' + DATE_PAT + r')', line)
        if date_m and "受付日" not in current:
            current["受付日"] = date_m.group(1)

        # 受付番号
        no_m = re.search(r'第([0-9]+)号', line)
        if no_m and "受付番号" not in current:
            current["受付番号"] = "第" + no_m.group(1) + "号"

        # 債権額 / 極度額
        kingaku_m = re.search(r'(?:債権額|極度額|債権極度額)\s+金([\d,，千百万億]+円)', line)
        if kingaku_m and "債権額" not in current:
            current["債権額"] = "金" + kingaku_m.group(1)

        # 債務者（住所と名前が1〜2行に分かれる）
        sm = re.search(r'債務者\s+(.+?)(?:\s*[│┃]|$)', line)
        if sm and "債務者" not in current:
            addr = sm.group(1).strip().rstrip('│┃').strip()
            # 次行以降から名前を探す
            name = _find_debtor_name(lines, i + 1)
            current["債務者"] = name or re.sub(r'\s+', '', addr)

        # 抵当権者 / 根抵当権者（複数行にまたがる住所に対応）
        if ("抵当権者" in line or "根抵当権者" in line) and "抵当権者" not in current:
            # まず同行末尾セルに名前があるか確認（番地なし都市型住所の場合は名前なし）
            cells = re.split(r'[│┃]', line)
            cell  = next((c.strip() for c in reversed(cells) if c.strip()), '')
            inline = re.sub(r'(?:根?抵当権者)\s*', '', cell).strip()
            inline_clean = re.sub(r'\s+', '', inline)
            # 住所でなく日本語名なら即採用（例: 「氏名 会社名」が同行に収まる場合）
            if (inline_clean
                    and re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', inline_clean)
                    and not re.search(r'[0-9]+番地|丁目|[0-9]+番[0-9]', inline_clean)):
                current["抵当権者"] = inline_clean
            else:
                # 次行以降を最大18行スキャンして名前を探す
                name = _find_teito_name(lines, i + 1)
                if name:
                    current["抵当権者"] = name

    if current:
        entries.append(current)

    active = [e for e in entries if e.get("順位") not in cancelled_ranks]
    data["抵当権件数"] = str(len(active))

    if active:
        data["抵当権債権額"] = SEP.join(
            e.get("債権額", "") for e in active if e.get("債権額")
        )
        data["抵当権債務者"] = SEP.join(
            e.get("債務者", "") for e in active if e.get("債務者")
        )
        data["抵当権者"] = SEP.join(
            e.get("抵当権者", "") for e in active if e.get("抵当権者")
        )

    return data

# ====================================================
# 共同担保目録パース（共通）
# ====================================================
def parse_kyodo_tanpo(tanpo_text: str) -> dict:
    data = {"共同担保一覧": ""}
    if not tanpo_text.strip():
        return data
    t = zen2han(tanpo_text)

    # エントリを番号行(┃ N│)で区切り、抹消済みブロックを除外
    lines = t.split('\n')
    blocks, current_block = [], []
    for line in lines:
        if re.search(r'┃\s*[0-9]+\s*[│┃]', line):
            if current_block:
                blocks.append(current_block)
            current_block = [line]
        elif current_block:
            current_block.append(line)
    if current_block:
        blocks.append(current_block)

    seen, unique = set(), []
    for block in blocks:
        block_text = '\n'.join(block)
        if '抹消' in block_text:
            continue

        # 第2列（担保の目的である権利の表示）を行ごとに収集
        col2_list = []
        for line in block:
            if '│' not in line:
                continue
            segs = line.split('│')
            if len(segs) >= 3:
                col2 = segs[1].strip()
                if col2:
                    col2_list.append(col2)

        if not col2_list:
            continue

        # スペース結合し、セル境界で分割された「土地」「建物」等を修復
        joined = ' '.join(col2_list)
        joined = re.sub(r'土 地', '土地', joined)
        joined = re.sub(r'建 物', '建物', joined)
        joined = re.sub(r'区分建 物', '区分建物', joined)

        # 最後の土地/建物/区分建物の位置
        m_end = -1
        for ptype in ['区分建物', '建物', '土地']:
            pos = joined.rfind(ptype)
            if pos != -1 and pos + len(ptype) > m_end:
                m_end = pos + len(ptype)
        if m_end == -1:
            continue

        # 直前の土地/建物/区分建物の終点を探して最新の記述範囲を絞り込む
        seg = joined[:m_end]
        prev_end = -1
        for ptype in ['区分建物', '建物', '土地']:
            pos = seg.rfind(ptype, 0, m_end - len(ptype) - 1)
            if pos != -1 and pos + len(ptype) > prev_end:
                prev_end = pos + len(ptype)

        desc = seg[prev_end + 1:].strip() if prev_end != -1 else seg.strip()

        # 市区町村名以降に絞り込む（法務局名・変更履歴のプレフィックスを除去）
        addr_m = re.search(r'[^\s]{2,}(?:市|郡)', desc)
        if addr_m:
            desc = desc[addr_m.start():].strip()

        # セル幅で分割された語を結合
        desc = re.sub(r'([0-9]) ([0-9])', r'\1\2', desc)   # "3 98番" → "398番"
        desc = re.sub(r'家屋 番号', '家屋番号', desc)
        desc = re.sub(r'家 屋番号', '家屋番号', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()

        if desc and re.search(r'(?:土地|建物|区分建物)', desc) and desc not in seen:
            seen.add(desc)
            unique.append(desc)

    if unique:
        data["共同担保一覧"] = SEP.join(unique)
    return data

# ====================================================
# メインパース関数
# ====================================================
def parse_touki(text: str, filename: str) -> tuple:
    """(record_dict, doc_type) を返す。doc_type: 'tochi' / 'tatemono' / 'other'"""
    t = zen2han(text)
    doc_type = detect_type(filename, text)

    if doc_type == 'other':
        return None, 'other'

    sec = split_sections(t)

    # ファイル名から「不動産登記（...）XXXXXXXX.PDF」以降を除去
    fname_short = re.sub(r'不動産登記.*$', '', filename).strip()
    record = {"ファイル名": fname_short or filename, "不動産番号": parse_fudosan_no(t)}

    if doc_type == 'tochi':
        record.update(parse_hyodai_tochi(sec["hyodai"] or t))
    else:
        record.update(parse_hyodai_tatemono(sec["hyodai"] or t))

    record.update(parse_kouku(sec["kouku"]))
    # 甲区で所有者が取れなかった場合、表題部の所有者欄を参照（国有地等）
    if not record.get("所有者氏名"):
        m = re.search(r'所\s*有\s*者[│┃]\s*([^\n│┃]{1,40})', sec["hyodai"] or t)
        if m:
            record["所有者氏名"] = re.sub(r'\s+', '', m.group(1)).strip()
    record.update(parse_otsuku(sec["otsuku"]))
    record.update(parse_kyodo_tanpo(sec["tanpo"]))
    record["抽出日時"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return record, doc_type

# ====================================================
# 甲区・乙区 全履歴パース（個別解析ウィンドウ用）
# ====================================================

def parse_kouku_history(kouku: str) -> list:
    """甲区の全権利ブロックを返す（付記を除く）。末尾が現在の所有者。"""
    if not kouku.strip():
        return []

    _CAUSE_PAT = (r'売買|相続|贈与|競売|公売|交換|合併|信託|財産分与|'
                  r'収用|判決|時効|遺産分割|換地処分|真正な登記名義の回復')

    lines = kouku.split('\n')
    rank_positions = [
        i for i, ln in enumerate(lines)
        if re.search(r'┃\s*[0-9]+\s*│', ln) and '付記' not in ln
    ]
    if not rank_positions:
        return []

    blocks = []
    for k, rp in enumerate(rank_positions):
        end         = rank_positions[k + 1] if k + 1 < len(rank_positions) else len(lines)
        block_lines = lines[rp:end]
        block_text  = '\n'.join(block_lines)

        rank_m  = re.search(r'┃\s*([0-9]+)\s*│', block_lines[0])
        rank_no = rank_m.group(1) if rank_m else "?"

        # 登記の目的：第1行の第2列（│区切り）から直接取得
        # ※ 「匂坂得郎持分一部移転」「所有権一部移転」等に正しく対応するため
        #   旧来のキーワード検索ではなく列解析を使う
        mokuteki = ""
        if block_lines:
            cells0 = re.split(r'[│┃]', block_lines[0])
            if len(cells0) >= 3 and cells0[2].strip():
                mokuteki = re.sub(r'\s+', '', cells0[2]).strip()
            # 登記の目的が2行に分かれる場合（例: "戸塚幸宏を除く共有者全員持分全部" + "移転"）
            if len(block_lines) > 1:
                cells1 = re.split(r'[│┃]', block_lines[1])
                cont = cells1[2].strip() if len(cells1) >= 3 else ""
                if (cont
                        and not cells1[1].strip()        # 順位列が空 = 継続行
                        and len(cont) <= 6               # 短い続き語のみ
                        and not re.search(r'[0-9]', cont)):  # 数字なし（受付番号と混同しない）
                    mokuteki += re.sub(r'\s+', '', cont)
        # フォールバック（列分割で取れない旧フォーマット対応）
        if not mokuteki:
            for ln in block_lines[:4]:
                m = re.search(
                    r'(所有権(?:一部)?(?:保存|移転|抹消)|持分(?:全部|一部)移転'
                    r'|差押え?|仮差押え?|仮登記|信託)',
                    ln
                )
                if m:
                    mokuteki = re.sub(r'\s+', '', m.group(1)).strip()
                    break

        # 受付年月日・番号
        rcp = re.findall(r'(' + DATE_PAT + r')[^\n]*\n?[^\n]*第([0-9]+)号', block_text)
        uketsuke_date = rcp[0][0] if rcp else ""
        uketsuke_no   = ("第" + rcp[0][1] + "号") if rcp else ""

        # 原因・日付
        cm = re.findall(
            r'原因\s+(' + DATE_PAT + r')[^\n│┃]{0,20}?(' + _CAUSE_PAT + r')',
            block_text, re.MULTILINE,
        )
        toroku_date  = cm[-1][0] if cm else uketsuke_date
        toroku_cause = cm[-1][1] if cm else ("保存" if '保存' in block_text else "")

        # 付記が始まる位置（付記以降は除外）
        fuki = next((i for i, ln in enumerate(block_lines) if re.search(r'┃\s*付記', ln)), None)
        main_lines = block_lines[:fuki] if fuki is not None else block_lines

        # 所有者・共有者
        names, addrs = [], []
        has_kyoyusha = any('共有者' in ln for ln in main_lines)

        if has_kyoyusha:
            SKIP_RE = re.compile(
                r'移記|規定|附則|令第|登記の目的|受付年月日|権利者その他|順位[0-9]'
                r'|付記|目 的|年月日|番 号|^[*＊]|下線'
            )
            _ADDR_RE = re.compile(r'番地|丁目|[0-9]+番[0-9]+号')
            cur_addr, cur_持分, prev_was_addr = '', '', False
            for line in main_lines:
                parts = re.split(r'[│┃]', line)
                cell  = next((p.strip() for p in reversed(parts) if p.strip()), '')
                if not cell or SKIP_RE.search(cell):
                    continue
                if '共有者' in cell or '所有者' in cell:
                    addr_part = re.sub(r'(共有者|所有者)\s*', '', cell).strip()
                    if _ADDR_RE.search(addr_part):
                        cur_addr = addr_part
                    cur_持分 = ''
                    prev_was_addr = False
                    continue
                if _ADDR_RE.search(cell):
                    cur_addr = cell
                    cur_持分 = ''
                    prev_was_addr = True
                    continue
                # 持分（万含み分数に対応: 5万5280分の5765）
                if re.search(r'[0-9]+(?:万[0-9]*)?分の[0-9]+', cell):
                    m2 = re.search(r'([0-9]+(?:万[0-9]*)?)分の([0-9]+)', cell)
                    if m2:
                        cur_持分 = f"{m2.group(1)}分の{m2.group(2)}"
                    prev_was_addr = False
                    continue
                clean = re.sub(r'\s+', '', cell)
                if (re.search(r'[一-鿿぀-ヿ]', clean)
                        and not re.search(r'[0-9]', clean)
                        and not re.search(r'番地|丁目|登記|移転|移記|換地|相続|売買|贈与|原因', clean)):
                    if prev_was_addr:
                        cur_addr += clean
                        prev_was_addr = False
                        continue
                    names.append(f"{clean}({cur_持分})" if cur_持分 else clean)
                    addrs.append(cur_addr)
                    cur_持分 = ''
                    prev_was_addr = False
        else:
            for i, line in enumerate(main_lines):
                if '所有者' not in line and '受託者' not in line:
                    continue
                am = re.search(r'(?:所有者|受託者)\s+([^│┃\n]+?)(?:\s*[│┃]|$)', line)
                if not am:
                    continue
                addr    = am.group(1).strip()
                持分_m  = re.search(r'持分([0-9]+分の[0-9]+)', line)
                if not 持分_m and i + 2 < len(main_lines):
                    持分_m = re.search(r'持分([0-9]+分の[0-9]+)', main_lines[i + 2])
                持分 = 持分_m.group(1) if 持分_m else ""
                name = ""
                for offset in [1, 2]:
                    if i + offset < len(main_lines):
                        cells = re.split(r'[│┃]', main_lines[i + offset])
                        name  = extract_jp_name(cells)
                        if name:
                            break
                # フォールバック：「所有者　浜松市」のように名前が同行にある場合
                if not name and addr:
                    addr_clean = re.sub(r'\s+', '', addr)
                    if (re.search(r'[一-鿿]', addr_clean)
                            and not re.search(r'[0-9]', addr_clean)
                            and not re.search(r'番地|丁目|番[0-9]|号室', addr_clean)):
                        name = addr_clean
                        addr = ""
                names.append(f"{name}({持分})" if (name and 持分) else (name or ""))
                addrs.append(addr)

        # 差押・仮差押の場合：所有者は取れないので債権者・申立人を差押名義人として抽出
        sashiosae_meigi = ""
        if not names:
            _SASHIPAT = re.compile(
                r'(?:債権者|申立人|申請人|参加差押申立人|交付要求者)'
            )
            for idx, ln in enumerate(main_lines):
                if not _SASHIPAT.search(ln):
                    continue
                # 同一行に「キーワード 名前」が含まれる場合
                m_inline = re.search(
                    r'(?:債権者|申立人|申請人|参加差押申立人|交付要求者)'
                    r'\s+([^│┃\n]{2,30}?)(?:\s*[│┃]|$)',
                    ln
                )
                if m_inline:
                    inline_clean = re.sub(r'\s+', '', m_inline.group(1)).strip()
                    # 住所（番地・丁目）は名前ではないのでスキップ
                    is_addr = bool(re.search(r'番地|丁目|[0-9]+番[0-9]', inline_clean))
                    if inline_clean and re.search(r'[一-鿿]', inline_clean) and not is_addr:
                        sashiosae_meigi = inline_clean
                        break
                # 翌行・翌々行から氏名を取得（住所の次の行に名前がある場合も対応）
                for off in [1, 2]:
                    if idx + off < len(main_lines):
                        cells_n = re.split(r'[│┃]', main_lines[idx + off])
                        name_n  = extract_jp_name(cells_n)
                        if name_n:
                            sashiosae_meigi = name_n
                            break
                if sashiosae_meigi:
                    break

        # 付記の更正があれば持分を正しい値に更新（錯誤更正：12分の1→40分の1等）
        if fuki is not None and names:
            in_corr      = False
            corr_by_name: dict = {}  # {氏名: 修正後持分} 形式2: NAME持分 XX分のYY
            corr_single  = ""        # 形式1: （正）持分XX分のYY（名前不明の一括更正）
            for ln in block_lines[fuki:]:
                if re.search(r'┃\s*付記', ln):
                    in_corr = '更正' in ln
                if not in_corr:
                    continue
                # 形式1: 「（正）持分XX分のYY」
                mc1 = re.search(r'[（(]正[）)]\s*持分\s*([0-9]+分の[0-9]+)', ln)
                if mc1:
                    corr_single = mc1.group(1)
                # 形式2: 「NAME持分 XX分のYY」 ── 実際の登記簿に多い形式
                for cell in re.split(r'[│┃]', ln):
                    mc2 = re.search(
                        r'([一-鿿]{2,12})\s*持分\s*([0-9]+分の[0-9]+)',
                        cell.strip()
                    )
                    if mc2:
                        corr_by_name[re.sub(r'\s+', '', mc2.group(1))] = mc2.group(2)
            bare_re = re.compile(r'[(（][^)）]*[)）]')
            if corr_by_name:
                # 名前ごとに個別更正を適用
                names = [
                    f"{bare_re.sub('', nm).strip()}({corr_by_name[bare_re.sub('', nm).strip()]})"
                    if bare_re.sub('', nm).strip() in corr_by_name else nm
                    for nm in names
                ]
            elif corr_single:
                # 全員に同一更正を一括適用
                names = [f"{bare_re.sub('', nm).strip()}({corr_single})" for nm in names]

        # 付記の名称変更・商号変更・氏名変更: 元名を保持し、変更後名を別ブロックとして追加
        _fuki_new_name = ""
        _fuki_meigi_mokuteki = ""
        if fuki is not None and names:
            _in_nc = False
            for ln in block_lines[fuki:]:
                if re.search(r'┃\s*付記', ln):
                    if re.search(r'(?:名称|氏名|商号)変更', ln):
                        _in_nc = True
                        _cells0 = re.split(r'[│┃]', ln)
                        if len(_cells0) >= 3 and _cells0[2].strip():
                            _fuki_meigi_mokuteki = re.sub(r'\s+', '', _cells0[2]).strip()
                    else:
                        _in_nc = False
                if not _in_nc:
                    continue
                for cell in re.split(r'[│┃]', ln):
                    mc = re.search(r'(?:商号|名称|氏名)\s+([^\n│┃]{2,30})', cell.strip())
                    if mc:
                        cand = re.sub(r'\s+', '', mc.group(1)).strip()
                        if cand and re.search(r'[\u4e00-\u9fff]', cand):
                            _fuki_new_name = cand

        _blk: dict = {
            "順位":       rank_no,
            "登記の目的": mokuteki,
            "受付年月日": uketsuke_date,
            "受付番号":   uketsuke_no,
            "取得日":     toroku_date,
            "取得原因":   toroku_cause,
            "所有者氏名": SEP.join(names) if names else "",
            "所有者住所": SEP.join(addrs) if addrs else "",
        }
        if sashiosae_meigi:
            _blk["差押名義人"] = sashiosae_meigi
        if _fuki_new_name:
            _blk["名称変更後"] = _fuki_new_name
        blocks.append(_blk)

        # 付記名称変更があれば変更登記として別ブロックを追加
        if _fuki_new_name and names:
            _bare_re3 = re.compile(r'[(（][^)）]*[)）]')
            _orig_bare = _bare_re3.sub('', names[0]).strip()
            blocks.append({
                "順位":       rank_no + "付記",
                "登記の目的": _fuki_meigi_mokuteki or "名義人名称変更",
                "受付年月日": uketsuke_date,
                "受付番号":   "",
                "取得日":     toroku_date,
                "取得原因":   "",
                "所有者氏名": _fuki_new_name,
                "所有者住所": SEP.join(addrs) if addrs else "",
                "元の氏名":   _orig_bare,
                "状態":       "変更",
                "_is_fuki_meigi": True,
            })

    # 所有権系エントリを暫定「現在」とする（base_agent でさらに精緻化）
    # 差押・更正・仮登記・信託等は「参考」として区別
    ownership_indices = [
        i for i, b in enumerate(blocks)
        if _KOUKU_OWNERSHIP_RE.search(b.get("登記の目的", ""))
    ]
    last_ownership_idx = ownership_indices[-1] if ownership_indices else -1

    _MEIGI_CHANGE_RE = re.compile(
        r'(?:登記)?名義人(?:表示変更|住所変更|氏名変更|名称変更|変更)'
        r'|名称変更|氏名変更'
    )
    _SASHIOSAE_MATSUSHO_RE = re.compile(
        r'差押(?:登記)?抹消|参加差押(?:登記)?抹消|仮差押(?:登記)?抹消'
    )
    _SASHIOSAE_RE = re.compile(
        r'差押え?|仮差押え?|参加差押え?'
    )
    for i, b in enumerate(blocks):
        if b.get("_is_fuki_meigi"):
            b["状態"] = "変更"
            continue
        mokuteki = b.get("登記の目的", "")
        if _KOUKU_OWNERSHIP_RE.search(mokuteki):
            b["状態"] = "現在" if i == last_ownership_idx else "移転前"
        elif _MEIGI_CHANGE_RE.search(mokuteki):
            b["状態"] = "変更"
        elif _SASHIOSAE_MATSUSHO_RE.search(mokuteki):
            b["状態"] = "差押抹消"
        elif _SASHIOSAE_RE.search(mokuteki):
            b["状態"] = "差押"
        else:
            b["状態"] = "参考"

    # ── 累積持分追跡 ──────────────────────────────────────────────────────────
    # 各ブロックに "_cumulative" (dict: {氏名: (分子, 分母)}) を付与し、
    # 現在ブロックの 所有者氏名 を累積値で上書きする
    from math import gcd as _gcf

    def _jf_parse(s):
        """「X分のY」→ (Y, X) = (分子, 分母). 持分なし → None"""
        m = re.search(r'\(([0-9]+)分の([0-9]+)\)', s)
        return (int(m.group(2)), int(m.group(1))) if m else None

    def _jf_add(a, b):
        n1, d1 = a; n2, d2 = b
        lc = d1 * d2 // _gcf(d1, d2)
        return (n1 * (lc // d1) + n2 * (lc // d2), lc)

    def _jf_sub(a, b):
        n1, d1 = a; n2, d2 = b
        lc = d1 * d2 // _gcf(d1, d2)
        n = n1 * (lc // d1) - n2 * (lc // d2)
        return (n, lc) if n > 0 else None

    def _bare_name(s):
        return re.sub(r'[(（][^)）]*[)）]', '', s).strip()

    cum: dict = {}   # {氏名: (分子, 分母)}
    addr_map: dict = {}  # {氏名: 住所}  ← 最後に登場した住所を保持

    own_blocks = [b for b in blocks if b.get("状態") in ("現在", "移転前")]
    for b in own_blocks:
        moku   = b.get("登記の目的", "")
        names  = [s for s in b.get("所有者氏名", "").split(SEP) if s.strip()]
        addrs  = [s for s in b.get("所有者住所", "").split(SEP)]

        # 住所マップを更新（最後に登場した住所を記録）
        for nm_raw, addr in zip(names, addrs + [""] * len(names)):
            bn = _bare_name(nm_raw)
            if bn and addr.strip():
                addr_map[bn] = addr.strip()

    # ── 付記住所変更・住所移転を addr_map に反映 ──────────────────────────────────
    # 「付記X号 登記名義人住所変更」のエントリで住所が更新される場合に対応
    _FUKI_ADDR_RE  = re.compile(r'丁目|番地|番[0-9]+号?')   # 住所断片の条件
    _FUKI_NOISE_RE = re.compile(                             # 除外すべきノイズ
        r'法務省令|附則第|規定により移記|の登記を移記'
        r'|^第[0-9]+号$'        # 受付番号（例: 第45617号）
        r'|^[0-9]+年[0-9]+月'   # 年月日単独行（移記日付）
    )
    _in_fuki_addr = False
    _fuki_target  = ""
    _fuki_parts: list = []
    for _ln in lines:
        # 新しい順位番号行（付記以外）→ 収集を打ち切り
        if re.search(r'┃\s*[0-9]+\s*[│┤]', _ln) and not re.search(r'付記', _ln):
            if _fuki_target and _fuki_parts:
                addr_map[_fuki_target] = ''.join(_fuki_parts)
            _in_fuki_addr = False
            _fuki_target  = ""
            _fuki_parts   = []
            continue
        # 付記行の開始
        if re.search(r'┃\s*付記', _ln):
            # 前エントリを確定
            if _fuki_target and _fuki_parts:
                addr_map[_fuki_target] = ''.join(_fuki_parts)
            _in_fuki_addr = bool(re.search(r'住所(?:変更|移転)', _ln))
            _fuki_target  = ""
            _fuki_parts   = []
            if not _in_fuki_addr:
                continue
        elif not _in_fuki_addr:
            continue
        # セルを走査して名前と住所を抽出
        for _cell in re.split(r'[│┃]', _ln):
            _cs = re.sub(r'\s+', '', _cell.strip())
            if not _cs:
                continue
            # ノイズセル（受付番号・法務省令テキスト）は除外
            if _FUKI_NOISE_RE.search(_cs):
                continue
            # 「共有者XXXの住所」「所有者XXXの住所」形式
            _nm_m = re.search(
                r'(?:共有者|所有者|登記名義人)\s*([一-鿿]{2,10})\s*の住所\s*(.*)',
                _cell
            )
            if _nm_m:
                _fuki_target = re.sub(r'\s+', '', _nm_m.group(1))
                _rest = re.sub(r'\s+', '', _nm_m.group(2)).strip()
                if _rest and not _FUKI_NOISE_RE.search(_rest):
                    _fuki_parts.append(_rest)
                continue
            # 住所断片（丁目・番地・番XX号）かつノイズなし
            if _FUKI_ADDR_RE.search(_cs) and not _FUKI_NOISE_RE.search(_cs):
                _fuki_parts.append(_cs)
    # ループ後の最終エントリを確定
    if _fuki_target and _fuki_parts:
        addr_map[_fuki_target] = ''.join(_fuki_parts)

    for b in own_blocks:
        moku  = b.get("登記の目的", "")
        names = [s for s in b.get("所有者氏名", "").split(SEP) if s.strip()]
        # 取引ごとに記録された氏名→持分を解析
        stated: dict = {}
        for nm_raw in names:
            bn  = _bare_name(nm_raw)
            frac = _jf_parse(nm_raw)
            if bn:
                stated[bn] = frac

        # ── 移転タイプ別に cum を更新 ──
        # 1) 所有権全部移転系（全リセット）:
        #    - 所有権移転/保存/登記, 換地処分, 仮登記に基づく本登記
        #    - 共有者全員持分全部移転（全員が一括移転）
        #    - 合併・更生計画による所有権移転
        if re.search(
            r'^(?:所有権(?:移転|保存|登記)|換地処分|仮登記に基づく本登記|共有者全員持分全部移転)$'
            r'|合併(?:による)?(?:所有権|持分)'
            r'|更生(?:計画|法)?(?:による)?(?:所有権|持分)',
            moku
        ):
            cum = {}
            for nm, fr in stated.items():
                cum[nm] = fr if fr else (1, 1)

        # 2) 所有権一部移転: 述べられた持分を追加、前保有者から差し引き
        elif re.search(r'^所有権一部移転$', moku):
            prev_sole = [k for k in cum if k not in stated]
            total_add = (0, 1)
            for nm, fr in stated.items():
                if fr:
                    cum[nm] = _jf_add(cum[nm], fr) if nm in cum else fr
                    total_add = _jf_add(total_add, fr)
            if len(prev_sole) == 1 and total_add[0] > 0:
                r = _jf_sub(cum.get(prev_sole[0], (0, 1)), total_add)
                if r:
                    cum[prev_sole[0]] = r
                elif prev_sole[0] in cum:
                    del cum[prev_sole[0]]

        # 3) XX持分(一部|全部)移転 / を除く共有者全員持分全部移転
        elif re.search(r'持分(?:一部|全部)移転|を除く(?:共有者全員持分全部移転|所有権)', moku):
            # 「を除く」の場合: 述べられた人 + 除外者を確認
            nozoku_m = re.match(r'^(.+?)を除く(?:共有者全員持分全部移転|所有権)', moku)
            if nozoku_m:
                excluded = nozoku_m.group(1).strip()
                total_add = (0, 1)
                for nm, fr in stated.items():
                    if fr:
                        cum[nm] = _jf_add(cum[nm], fr) if nm in cum else fr
                        total_add = _jf_add(total_add, fr)
                # 除外者の持分はそのまま（変化なし）
                # 移転した分は除外者以外の全員の持分が合計 total_add → 既にcumに反映
                # 移転元（除外者以外の旧所有者）から差し引き
                for prev_nm in list(cum.keys()):
                    if prev_nm == excluded or prev_nm in stated:
                        continue
                    # この人は全部移転したはず（0になる）
                    del cum[prev_nm]
            else:
                # 通常の持分移転: 移転者を特定（複数名が「、」区切りの場合も対応）
                xfer_m = re.match(r'^(.+?)持分(一部|全部)移転$', moku)
                transferor = xfer_m.group(1).strip() if xfer_m else ""
                is_all = xfer_m.group(2) == '全部' if xfer_m else False
                total_add = (0, 1)
                for nm, fr in stated.items():
                    if fr:
                        cum[nm] = _jf_add(cum[nm], fr) if nm in cum else fr
                        total_add = _jf_add(total_add, fr)
                for _s in re.split(r'[、・,，]', transferor) if transferor else []:
                    _s = _s.strip()
                    if not _s:
                        continue
                    if is_all:
                        if _s in cum:
                            cum.pop(_s)
                        elif len(_s) >= 3:
                            given = _s[-3:]
                            _matched = [k for k in cum if k != _s and k.endswith(given) and len(k) == len(_s)]
                            if len(_matched) == 1:
                                cum.pop(_matched[0])
                    elif _s in cum and total_add[0] > 0:
                        r = _jf_sub(cum[_s], total_add)
                        if r:
                            cum[_s] = r
                        else:
                            del cum[_s]

        # 累積スナップショットをブロックに保存
        b["_cumulative"] = {k: v for k, v in cum.items() if v[0] > 0}

    # 現在ブロックの住所を確定
    # 優先順位: ①ブロック自身の解析済み住所 > ②addr_map（fuki更新含む）
    # 理由: 現在ブロックに直接記載された住所が最新の登記住所を示す
    #   (例: 行政区再編で 東区→中央区 に変わった場合、現在エントリに中央区が記載されるが
    #    付記住所変更には東区のままのことがある)
    _blk_bare = re.compile(r'[(（][^)）]*[)）]')
    for b in blocks:
        if b.get("状態") == "現在" and b.get("_cumulative"):
            _cum = b["_cumulative"]
            # ブロック自身の住所リストを氏名→住所マップに変換
            _own_names = [s.strip() for s in b.get("所有者氏名", "").split(SEP) if s.strip()]
            _own_addrs = [s.strip() for s in b.get("所有者住所", "").split(SEP)]
            _blk_addr: dict = {}
            for _nm_r, _ad in zip(_own_names, _own_addrs + [""] * len(_own_names)):
                _bn = _blk_bare.sub('', _nm_r).strip()
                if _bn and _ad:
                    _blk_addr[_bn] = _ad
            # 各所有者: ブロック住所優先、なければ addr_map
            b["所有者住所"] = SEP.join(
                _blk_addr.get(nm) or addr_map.get(nm, "")
                for nm in _cum
            )

    # ── 付記名称変更を _cumulative に反映 ──────────────────────────────────────
    # 「2付記X号 登記名義人名称変更」等で社名・氏名が変更された場合、
    # _cumulative のキー（旧名）を新名に置換して「現在の所有者」カードに正しく表示させる
    for _nb in blocks:
        if not (_nb.get("_is_fuki_meigi")
                and _nb.get("元の氏名")
                and _nb.get("所有者氏名")):
            continue
        _old_key = _nb["元の氏名"]
        _new_key = _nb["所有者氏名"]
        for _tgt in blocks:
            if "_cumulative" not in _tgt:
                continue
            if _old_key in _tgt["_cumulative"]:
                _tgt["_cumulative"][_new_key] = _tgt["_cumulative"].pop(_old_key)

    return blocks


def parse_kyodo_tanpo_history(tanpo_text: str) -> list:
    """共同担保目録の全エントリを返す（抹消済み含む）"""
    if not tanpo_text.strip():
        return []
    t = zen2han(tanpo_text)

    lines = t.split('\n')

    # 記号及び番号を行ごとに追跡（複数目録の区別用）
    # PDFレイアウト由来のスペースを許容するパターン
    _KIGOU_PAT = re.compile(r'記\s*号\s*及\s*び\s*番\s*号[│┃\s]+([^\n│┃]+)')
    current_kigou = ""
    line_kigou = []
    for line in lines:
        m = _KIGOU_PAT.search(line)
        if m:
            current_kigou = re.sub(r'\s+', '', m.group(1)).strip()
        line_kigou.append(current_kigou)

    blocks, current_block, current_block_start = [], [], 0
    for i, line in enumerate(lines):
        if re.search(r'┃\s*[0-9]+\s*[│┃]', line):
            if current_block:
                blocks.append((current_block, line_kigou[current_block_start]))
            current_block = [line]
            current_block_start = i
        elif current_block:
            current_block.append(line)
    if current_block:
        blocks.append((current_block, line_kigou[current_block_start]))

    result = []
    for block, kigou in blocks:
        # 抹消判定：│のある行の予備列(4列目)のみを確認してフッター誤検知を回避
        is_cancelled = False
        for line in block:
            if '│' not in line:
                continue
            segs = line.split('│')
            if len(segs) >= 4 and '抹消' in segs[3]:
                is_cancelled = True
                break

        col2_list = []
        for line in block:
            if '│' not in line:
                continue
            segs = line.split('│')
            if len(segs) >= 3:
                col2 = segs[1].strip()
                if col2:
                    col2_list.append(col2)

        if not col2_list:
            continue

        joined = ' '.join(col2_list)
        joined = re.sub(r'土 地', '土地', joined)
        joined = re.sub(r'建 物', '建物', joined)
        joined = re.sub(r'区分建 物', '区分建物', joined)

        m_end = -1
        for ptype in ['区分建物', '建物', '土地']:
            pos = joined.rfind(ptype)
            if pos != -1 and pos + len(ptype) > m_end:
                m_end = pos + len(ptype)
        if m_end == -1:
            continue

        seg      = joined[:m_end]
        prev_end = -1
        for ptype in ['区分建物', '建物', '土地']:
            pos = seg.rfind(ptype, 0, m_end - len(ptype) - 1)
            if pos != -1 and pos + len(ptype) > prev_end:
                prev_end = pos + len(ptype)

        desc = seg[prev_end + 1:].strip() if prev_end != -1 else seg.strip()

        addr_m = re.search(r'[^\s]{2,}(?:市|郡)', desc)
        if addr_m:
            desc = desc[addr_m.start():].strip()

        desc = re.sub(r'([0-9]) ([0-9])', r'\1\2', desc)
        desc = re.sub(r'家屋 番号', '家屋番号', desc)
        desc = re.sub(r'家 屋番号', '家屋番号', desc)
        desc = re.sub(r'\s+', ' ', desc).strip()

        if desc and re.search(r'(?:土地|建物|区分建物)', desc):
            result.append({
                "内容": desc,
                "状態": "抹消済み" if is_cancelled else "現在",
                "記号及び番号": kigou,
            })

    # 「全部抹消」行が目録レベルで存在する場合、その記号及び番号の全エントリを抹消済みにする
    # （個別エントリに抹消が記録されず目録末尾行のみに「全部抹消」が現れるケースに対応）
    cancelled_kigou: set = set()
    current_kigou_chk = ""
    for line in lines:
        km = _KIGOU_PAT.search(line)
        if km:
            current_kigou_chk = re.sub(r'\s+', '', km.group(1)).strip()
        if current_kigou_chk and '全部抹消' in line:
            cancelled_kigou.add(current_kigou_chk)
    if cancelled_kigou:
        for entry in result:
            if entry.get("記号及び番号") in cancelled_kigou:
                entry["状態"] = "抹消済み"

    return result


def parse_otsuku_history(otsuku: str) -> list:
    """乙区の全抵当権エントリを返す（抹消済み含む）"""
    if not otsuku.strip():
        return []

    # PUA文字（登記情報PDFの特殊フォントエンコーディング）を可読文字に変換
    # U+E178=(あ), U+E179=(い), U+E17A=(う), ...
    _PUA_KANA = {'': '(あ)', '': '(い)', '': '(う)',
                 '': '(え)', '': '(お)'}
    otsuku = re.sub(r'[-]', lambda m: _PUA_KANA.get(m.group(), ''), otsuku)

    lines   = otsuku.split('\n')
    entries = []
    current: dict = {}
    current_block_lines: list = []
    cancelled_ranks: set = set()

    # 目録 直後に文字化けPUA文字が挿入される場合も許容（.*? で吸収）
    _TANPO_REF_PAT = re.compile(
        r'共\s*同\s*担\s*保\s*目\s*録.*?(第[0-9][0-9/]*号)'
    )

    def _flush_block():
        """ブロック終了時に共担目録番号をブロック全体から抽出する"""
        if not current or "共担目録番号" in current:
            return
        block_flat = ' '.join(current_block_lines)
        m = _TANPO_REF_PAT.search(block_flat)
        if m:
            current["共担目録番号"] = m.group(1)   # 「第XXXX号」のみ保存（プレフィックス省略）
            print(f"  [共担目録] 順位{current.get('順位')}: {current['共担目録番号']}")
        else:
            # マッチしない場合 — "共同担保" が含まれる行を出力してパターン確認
            for ln in current_block_lines:
                if '共同担保' in ln or '担保' in ln:
                    print(f"  [共担目録 未取得] line: {repr(ln[:100])}")

    _cancel_flat = re.sub(r'\s*\n\s*', ' ', otsuku)
    for ce in re.findall(_OTSUKU_CANCEL_PAT, _cancel_flat):
        for num in re.findall(r'([0-9]+)\s*番', ce):
            cancelled_ranks.add(num)

    # 抹消エントリから抹消日・抹消受付番号を取得（cancelled_rank → dict）
    _CANCEL_ENTRY_LINE_RE = re.compile(
        r'[┃│]\s*[0-9]+\s*[│┃]'
        r'[^│┃]*?'
        r'((?:[0-9]+\s*番)(?:(?:[、,]\s*[0-9]+\s*番)*))'
        r'(?:根?抵当権|地上権|賃借権|差押え?|仮差押え?'
        r'|配偶者居住権|質権|地役権|区分地上権|永小作権)'
        r'(?:抹消|解除)'
    )
    cancel_info: dict = {}
    _blk_lines: list = []
    _blk_ranks: list = []

    def _flush_cancel_blk():
        if not _blk_ranks:
            return
        flat_b = ' '.join(_blk_lines)
        dm = re.search(r'(' + DATE_PAT + r')', flat_b)
        nm = re.search(r'第([0-9]+)号', flat_b)
        for rk in _blk_ranks:
            cancel_info[rk] = {
                "抹消日":   dm.group(1)              if dm else "",
                "抹消番号": "第" + nm.group(1) + "号" if nm else "",
            }
        _blk_lines.clear()
        _blk_ranks.clear()

    for line in lines:
        clm = _CANCEL_ENTRY_LINE_RE.search(line)
        if clm:
            _flush_cancel_blk()
            _blk_ranks.extend(re.findall(r'([0-9]+)\s*番', clm.group(1)))
            _blk_lines.append(line)
        elif _blk_ranks:
            # 別エントリ行が来たらブロック終了
            if re.search(r'[┃│]\s*[0-9]+\s*[│┃]', line):
                _flush_cancel_blk()
            else:
                _blk_lines.append(line)
    _flush_cancel_blk()

    current_fuki: dict = {}    # 現在構築中の付記エントリ
    pending_fuki: list = []    # 親エントリに紐づく付記エントリリスト

    for i, line in enumerate(lines):
        rank_m = _OTSUKU_ENTRY_RE.search(line)
        fuki_m = None if rank_m else _OTSUKU_FUKI_RE.search(line)

        if rank_m:
            # 付記を pending にまとめてから親エントリをフラッシュ
            if current_fuki:
                pending_fuki.append(current_fuki)
                current_fuki = {}
            if current:
                _flush_block()
                entries.append(current)
                entries.extend(pending_fuki)
            pending_fuki = []
            # 登記の目的を第2列（│区切り）から直接取得
            cells_r = re.split(r'[│┃]', line)
            mokuteki_r = re.sub(r'\s+', '', cells_r[2]).strip() if len(cells_r) >= 3 else ""
            current = {"順位": rank_m.group(1), "登記の目的": mokuteki_r}
            current_block_lines = [line]
        elif fuki_m and current:
            # 付記エントリ検出: 前の付記をキューに入れて新規開始
            if current_fuki:
                pending_fuki.append(current_fuki)
            parent_rank = fuki_m.group(1)
            fuki_no = fuki_m.group(2)
            cells_f = re.split(r'[│┃]', line)
            mokuteki_f = re.sub(r'\s+', '', cells_f[2]).strip() if len(cells_f) >= 3 else ""
            current_fuki = {
                "順位": f"{parent_rank}付記{fuki_no}",
                "登記の目的": mokuteki_f,
                "parent_順位": parent_rank,
                "_is_fuki": True,
            }
            current_block_lines.append(line)
        elif current:
            current_block_lines.append(line)

        # フィールド抽出: 付記中は current_fuki を優先して更新
        _tgt = current_fuki if current_fuki else (current if current else None)
        if not _tgt:
            continue

        date_m = re.search(r'(' + DATE_PAT + r')', line)
        if date_m and "受付日" not in _tgt:
            _tgt["受付日"] = date_m.group(1)

        no_m = re.search(r'第([0-9]+)号', line)
        if no_m and "受付番号" not in _tgt:
            _tgt["受付番号"] = "第" + no_m.group(1) + "号"

        # 債権額・債務者は親エントリのみに適用
        if _tgt is current:
            km = re.search(r'(?:債権額|極度額|債権極度額)\s+金([\d,，千百万億]+円)', line)
            if km and "債権額" not in current:
                current["債権額"] = "金" + km.group(1)

            sm = re.search(r'債務者\s+(.+?)(?:\s*[│┃]|$)', line)
            if sm and "債務者" not in current:
                addr = sm.group(1).strip().rstrip('│┃').strip()
                name = _find_debtor_name(lines, i + 1)
                current["債務者"] = name or re.sub(r'\s+', '', addr)

        if ("抵当権者" in line or "根抵当権者" in line) and "抵当権者" not in _tgt:
            cells = re.split(r'[│┃]', line)
            cell  = next((c.strip() for c in reversed(cells) if c.strip()), '')
            inline = re.sub(r'(?:根?抵当権者)\s*', '', cell).strip()
            inline_clean = re.sub(r'\s+', '', inline)
            if (inline_clean
                    and re.search(r'[一-鿿぀-ヿ]', inline_clean)
                    and not re.search(r'[0-9]+番地|丁目|[0-9]+番[0-9]', inline_clean)):
                _tgt["抵当権者"] = inline_clean
            else:
                name = _find_teito_name(lines, i + 1)
                if name:
                    _tgt["抵当権者"] = name

    # 最後の付記・親エントリをフラッシュ
    if current_fuki:
        pending_fuki.append(current_fuki)
    if current:
        _flush_block()
        entries.append(current)
        entries.extend(pending_fuki)

    # (あ)(い) サフィックスを除いたベース順位でも照合できるよう正規化ヘルパー
    _rank_base = lambda r: re.sub(r'\([あ-んア-ン]\)$', '', r).strip()

    for e in entries:
        rank_key   = e.get("順位", "")
        parent_key = e.get("parent_順位", "")
        is_cancelled = (rank_key in cancelled_ranks or
                        _rank_base(rank_key) in cancelled_ranks or
                        parent_key in cancelled_ranks or
                        _rank_base(parent_key) in cancelled_ranks)
        e["状態"] = "抹消済み" if is_cancelled else "現在"
        if is_cancelled:
            # 付記は親順位で抹消日を引く
            info = (cancel_info.get(rank_key) or
                    cancel_info.get(_rank_base(rank_key)) or
                    cancel_info.get(parent_key) or
                    cancel_info.get(_rank_base(parent_key), {}))
            if info.get("抹消日"):
                e["抹消日"] = info["抹消日"]
            if info.get("抹消番号"):
                e["抹消番号"] = info["抹消番号"]

    return entries


# ====================================================
# CSV列定義
# ====================================================
CSV_FIELDS_TOCHI = [
    "ファイル名", "不動産番号", "所在", "地番", "地目", "地積_m2",
    "所有者氏名", "所有者住所", "現在の所有者",
    "取得原因", "取得日", "受付年月日", "受付番号",
    "抵当権件数", "抵当権債権額", "抵当権債務者", "抵当権者",
    "共同担保一覧", "抽出日時",
]
CSV_FIELDS_TATEMONO = [
    "ファイル名", "不動産番号", "所在", "家屋番号", "種類", "構造", "床面積_m2",
    "所有者氏名", "所有者住所", "現在の所有者",
    "取得原因", "取得日", "受付年月日", "受付番号",
    "抵当権件数", "抵当権債権額", "抵当権債務者", "抵当権者",
    "共同担保一覧", "抽出日時",
]

# ====================================================
# SQLite 初期化
# ====================================================
def init_db(conn):
    for table, fields in [("touki_tochi", CSV_FIELDS_TOCHI),
                           ("touki_tatemono", CSV_FIELDS_TATEMONO)]:
        cols = "\n".join([f'    "{f}" TEXT,' for f in fields if f != "ファイル名"])
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT UNIQUE,
                {cols}
                "ファイル名" TEXT
            )
        """)
        # スキーマ変更マイグレーション: 新列を自動追加
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
        for f in fields:
            if f not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN "{f}" TEXT DEFAULT ""')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_files (
            file_path    TEXT PRIMARY KEY,
            file_hash    TEXT,
            doc_type     TEXT,
            processed_at TEXT
        )
    """)
    conn.commit()

def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

# このファイルはライブラリとして使用します。
# バッチ処理のエントリポイントは router.py を使用してください。
