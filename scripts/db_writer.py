"""
DB / CSV 書き込みモジュール（スレッドセーフ）

出力フォルダ構成:
  output_csv/
    tochi_all.csv          ← DB全件（常に最新・上書き）
    tatemono_all.csv       ← DB全件（常に最新・上書き）
    diff/
      tochi_diff_YYYYMMDD_HHMMSS.csv    ← 今回新規分のみ（履歴）
      tatemono_diff_YYYYMMDD_HHMMSS.csv ← 今回新規分のみ（履歴）
"""
import csv
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from touki_parser import (
    CSV_FIELDS_TOCHI, CSV_FIELDS_TATEMONO,
    init_db, setup_dirs, DB_PATH, CSV_DIR, REPORT_DIR,
)

setup_dirs()  # 必要なディレクトリを確実に作成

DIFF_DIR = CSV_DIR / "diff"


class DbWriter:
    def __init__(self, db_path: Path = DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        init_db(self._conn)
        DIFF_DIR.mkdir(parents=True, exist_ok=True)

    def write_record(self, record: dict, doc_type: str, pdf_path: Path, fhash: str):
        table    = "touki_tochi"    if doc_type == "tochi" else "touki_tatemono"
        fields   = CSV_FIELDS_TOCHI if doc_type == "tochi" else CSV_FIELDS_TATEMONO
        all_cols = ["file_hash"] + fields
        col_str      = ",".join([f'"{c}"' for c in all_cols])
        placeholders = ",".join(["?"] * len(all_cols))
        values = [fhash] + [record.get(c, "") for c in fields]

        with self._lock:
            self._conn.execute(
                f'INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})',
                values,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO processed_files VALUES (?,?,?,?)",
                (str(pdf_path), fhash, doc_type, datetime.now().isoformat()),
            )
            self._conn.commit()

    def is_processed(self, pdf_path: Path, fhash: str) -> bool:
        row = self._conn.execute(
            "SELECT file_hash FROM processed_files WHERE file_path=?",
            (str(pdf_path),),
        ).fetchone()
        return bool(row and row[0] == fhash)

    def write_csv(self, records: list, doc_type: str, today: str):
        """今回新規分のみ diff/ フォルダに書き出す"""
        label  = "tochi" if doc_type == "tochi" else "tatemono"
        fields = CSV_FIELDS_TOCHI if doc_type == "tochi" else CSV_FIELDS_TATEMONO

        if records:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            diff_path = DIFF_DIR / f"{label}_diff_{timestamp}.csv"
            with open(diff_path, "w", encoding="utf-8", newline="") as f:
                f.write('﻿')  # BOM（Excel で文字化けしないように）
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(records)
            print(f"[差分CSV] {diff_path.name}  {len(records)}件")

        # 全件CSVを常に再生成（DBから読み直し）
        self._write_all_csv(doc_type)

    def _write_all_csv(self, doc_type: str):
        """DBの全件を tochi_all.csv / tatemono_all.csv に書き出す（上書き）"""
        table    = "touki_tochi"    if doc_type == "tochi" else "touki_tatemono"
        fields   = CSV_FIELDS_TOCHI if doc_type == "tochi" else CSV_FIELDS_TATEMONO
        label    = "tochi"          if doc_type == "tochi" else "tatemono"
        all_path = CSV_DIR / f"{label}_all.csv"

        col_str = ",".join([f'"{c}"' for c in fields])
        rows    = self._conn.execute(
            f'SELECT {col_str} FROM {table} ORDER BY "抽出日時" DESC'
        ).fetchall()

        with open(all_path, "w", encoding="utf-8", newline="") as f:
            f.write('﻿')  # BOM（Excel で文字化けしないように）
            writer = csv.writer(f)
            writer.writerow(fields)
            writer.writerows(rows)

        print(f"[全件CSV] {all_path.name}  {len(rows)}件（DB全件）")

    def write_report(self, new_tochi: list, new_tatemono: list,
                     skip_count: int, today: str):
        report_path = REPORT_DIR / f"update_{today}.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# 登記簿処理レポート {today}\n\n")
            f.write(f"- 土地: {len(new_tochi)}件\n")
            f.write(f"- 建物: {len(new_tatemono)}件\n")
            f.write(f"- スキップ: {skip_count}件\n\n")
            for label, records, fields in [
                ("土地", new_tochi, CSV_FIELDS_TOCHI),
                ("建物", new_tatemono, CSV_FIELDS_TATEMONO),
            ]:
                if records:
                    f.write(f"## {label}\n\n")
                    for r in records:
                        key = r.get("地番") or r.get("家屋番号") or "?"
                        f.write(f"### {key} ({r.get('所在', '')})\n")
                        for k in fields:
                            v = r.get(k, "")
                            if v and k != "ファイル名":
                                f.write(f"- {k}: {v}\n")
                        f.write("\n")

    def close(self):
        self._conn.close()
