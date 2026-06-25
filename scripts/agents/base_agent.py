"""
エージェント基底クラス
土地・建物エージェントの共通処理をまとめる
"""
import re
import sys
from datetime import datetime
from fractions import Fraction
from math import lcm as _math_lcm
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from touki_parser import (
    extract_text, zen2han, split_sections,
    parse_fudosan_no, parse_kouku, parse_otsuku, parse_kyodo_tanpo,
    parse_kouku_history, parse_otsuku_history, parse_kyodo_tanpo_history,
)


def _accumulate_mochikomi(kouku_hist: list, sep: str = '；') -> dict:
    """甲区全ブロックを時系列で処理し {氏名: Fraction} を返す（持分累積計算）"""
    holdings: dict = {}
    _bare = re.compile(r'[(（][^)）]*[)）]')

    def bn(s: str) -> str:
        return _bare.sub('', s).strip()

    def _parse_num(n: str) -> int:
        """'5万5280' → 55280、通常数字はそのまま int 変換"""
        m = re.match(r'([0-9]+)万([0-9]*)', n)
        if m:
            return int(m.group(1)) * 10000 + (int(m.group(2)) if m.group(2) else 0)
        return int(n) if n.isdigit() else 0

    def pf(s: str) -> Fraction:
        m = re.search(r'([0-9]+(?:万[0-9]*)?)分の([0-9]+)', s)
        if not m:
            return Fraction(0)
        denom = _parse_num(m.group(1))
        numer = int(m.group(2))
        return Fraction(numer, denom) if denom > 0 and numer <= denom else Fraction(0)

    for b in kouku_hist:
        if b.get('状態') == '参考':
            continue
        mokuteki  = b.get('登記の目的', '')
        names_raw = [nm.strip() for nm in b.get('所有者氏名', '').split(sep) if nm.strip()]
        if not names_raw:
            continue
        pairs = [(bn(nm), pf(nm)) for nm in names_raw]

        if re.search(r'所有権一部移転', mokuteki):
            total = sum(f for _, f in pairs if f > 0)
            if total > 0 and holdings:
                top = max(holdings, key=lambda k: holdings[k])
                holdings[top] = max(Fraction(0), holdings[top] - total)
                if holdings[top] == 0:
                    del holdings[top]
            for nm, frac in pairs:
                if nm and frac > 0:
                    holdings[nm] = holdings.get(nm, Fraction(0)) + frac

        elif re.search(r'所有権(?:移転|保存)|共有者全員持分全部移転|仮登記に基づく本登記', mokuteki):
            holdings.clear()
            for nm, frac in pairs:
                if nm:
                    holdings[nm] = frac if frac > 0 else Fraction(1)

        else:
            m = re.search(r'^(.+?)持分(全部|一部)移転', mokuteki)
            if not m:
                continue
            src = bn(m.group(1))
            if m.group(2) == '全部':
                for _s in re.split(r'[、・,，]', src):
                    _s = _s.strip()
                    if _s in holdings:
                        holdings.pop(_s)
                    elif len(_s) >= 3:
                        # 改姓（婚姻等）対応: 下の名前（末尾2〜3字）が同じキーを探す
                        given = _s[-3:]
                        matched = [k for k in holdings if k != _s and k.endswith(given) and len(k) == len(_s)]
                        if len(matched) == 1:
                            holdings.pop(matched[0])
            else:
                total = sum(f for _, f in pairs if f > 0)
                if total > 0 and src in holdings:
                    holdings[src] = max(Fraction(0), holdings[src] - total)
                    if holdings[src] == 0:
                        del holdings[src]
            for nm, frac in pairs:
                if nm and frac > 0:
                    holdings[nm] = holdings.get(nm, Fraction(0)) + frac

    return {k: v for k, v in holdings.items() if v > 0}


class BaseAgent:
    """土地 / 建物エージェントの共通基底クラス"""

    doc_type: str = ""  # サブクラスで 'tochi' or 'tatemono' を設定

    def parse_hyodai(self, hyodai: str, full_text: str) -> dict:
        """表題部パース（サブクラスで実装）"""
        raise NotImplementedError

    def run(self, pdf_path: Path, fhash: str) -> dict | None:
        """
        PDFを受け取り、パース済みレコードと付随情報を返す。
        エラー時は None を返す。
        """
        try:
            text = extract_text(pdf_path)
            t    = zen2han(text)
            sec  = split_sections(t)

            fname_short = re.sub(r'不動産登記.*$', '', pdf_path.name).strip()
            record = {
                "ファイル名":   fname_short or pdf_path.name,
                "不動産番号": parse_fudosan_no(t),
            }

            # 表題部（土地 or 建物で異なる）
            record.update(self.parse_hyodai(sec["hyodai"] or t, t))

            # 甲区・乙区・共同担保（共通）
            record.update(parse_kouku(sec["kouku"]))

            # 甲区で所有者が取れなかった場合のフォールバック
            if not record.get("所有者氏名"):
                m = re.search(
                    r'所\s*有\s*者[│┃]\s*([^\n│┃]{1,40})',
                    sec["hyodai"] or t,
                )
                if m:
                    record["所有者氏名"] = re.sub(r'\s+', '', m.group(1)).strip()

            record.update(parse_otsuku(sec["otsuku"]))
            record.update(parse_kyodo_tanpo(sec["tanpo"]))
            record["抽出日時"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            kouku_hist = parse_kouku_history(sec["kouku"])

            # 持分累積計算と甲区 状態の再判定
            _SEP   = '；'
            _OWNRE = re.compile(
                r'所有権(?:一部)?(?:移転|保存)'
                r'|持分(?:全部|一部)移転'
                r'|共有者全員持分全部移転'
                r'|仮登記に基づく本登記'
            )
            if kouku_hist:
                # 累積持分を計算して record["所有者氏名"] を正確な値に更新
                accum = _accumulate_mochikomi(kouku_hist, _SEP)
                if accum:
                    # 表示分母は履歴中の全分母の LCM で決定（簡約防止：20/40→1/2にしない）
                    denom = 1
                    for b in kouku_hist:
                        for nm in b.get('所有者氏名', '').split(_SEP):
                            _m = re.search(r'([0-9]+)分の([0-9]+)', nm)
                            if _m:
                                denom = _math_lcm(denom, int(_m.group(1)))
                    for f in accum.values():
                        denom = _math_lcm(denom, f.denominator)
                    new_parts = [
                        nm if (denom == 1 or frac == Fraction(1))
                        else f"{nm}({denom}分の{int(frac * denom)})"
                        for nm, frac in accum.items()
                    ]
                    record["所有者氏名"] = _SEP.join(new_parts)

                cur_names: set = {
                    re.sub(r'\([^)]*\)', '', nm).strip()
                    for nm in record.get("所有者氏名", "").split(_SEP)
                    if nm.strip()
                }
                latest_idx: dict = {}
                for i, b in enumerate(kouku_hist):
                    if b.get("状態") == "参考":
                        continue
                    for nm_raw in b.get("所有者氏名", "").split(_SEP):
                        base = re.sub(r'\([^)]*\)', '', nm_raw).strip()
                        if base in cur_names:
                            latest_idx[base] = i
                for i, b in enumerate(kouku_hist):
                    if b.get("状態") == "参考":
                        continue
                    if not _OWNRE.search(b.get("登記の目的", "")):
                        continue
                    is_latest = any(
                        re.sub(r'\([^)]*\)', '', nm_raw).strip() in cur_names
                        and latest_idx.get(re.sub(r'\([^)]*\)', '', nm_raw).strip()) == i
                        for nm_raw in b.get("所有者氏名", "").split(_SEP)
                    )
                    b["状態"] = "現在" if is_latest else "移転前"

            # record["所有者住所"] を現在所有者リストの順に合わせて再構築
            # （parse_kouku は最後ブロックの住所のみ返すため複数オーナー時にズレる）
            if kouku_hist:
                _bare_re = re.compile(r'[(（][^)）]*[)）]')
                _addr_map: dict = {}
                for _b in kouku_hist:
                    if _b.get("状態") not in ("現在", "移転前"):
                        continue
                    _nms  = [s.strip() for s in _b.get("所有者氏名", "").split(_SEP) if s.strip()]
                    _adds = [s.strip() for s in _b.get("所有者住所", "").split(_SEP)]
                    for _nm, _ad in zip(_nms, _adds + [""] * len(_nms)):
                        _bn = _bare_re.sub('', _nm).strip()
                        if _bn and _ad:
                            _addr_map[_bn] = _ad
                # 現在ブロックの _cumulative 順と同じ順で住所を並べる
                _cur_blk = next(
                    (b for b in reversed(kouku_hist) if b.get("状態") == "現在"), None
                )
                if _cur_blk and _cur_blk.get("_cumulative"):
                    _cum_names = list(_cur_blk["_cumulative"].keys())
                    record["所有者住所"] = _SEP.join(
                        _addr_map.get(nm, "") for nm in _cum_names
                    )

            # 付記名称変更があれば record["所有者氏名"] の名前も更新
            for _b in kouku_hist:
                if _b.get("_is_fuki_meigi") and _b.get("元の氏名") and _b.get("所有者氏名"):
                    _old_nm = _b["元の氏名"]
                    _new_nm = _b["所有者氏名"]
                    _cur = record.get("所有者氏名", "")
                    if _old_nm in _cur:
                        record["所有者氏名"] = _cur.replace(_old_nm, _new_nm)

            # 所有権敷地権（マンション敷地）の場合: 直接の所有者名はなく区分所有者全員が共有
            if not record.get("所有者氏名") and re.search(r'所有権敷地権', sec.get("kouku", "")):
                record["所有者氏名"] = "（マンション敷地権）"

            # 現在の所有者が特定できた場合に○を付与（ピボット抽出用）
            record["現在の所有者"] = "○" if record.get("所有者氏名") else ""

            return {
                "record":   record,
                "doc_type": self.doc_type,
                "pdf_path": pdf_path,
                "fhash":    fhash,
                "history": {
                    "kouku":  kouku_hist,
                    "otsuku": parse_otsuku_history(sec["otsuku"]),
                    "tanpo":  parse_kyodo_tanpo_history(sec["tanpo"]),
                },
            }

        except Exception as e:
            import traceback
            print(f"  [ERROR] {pdf_path.name}: {e}")
            traceback.print_exc()
            return None
