"""
建物エージェント
表題部の建物固有フィールド（所在・家屋番号・種類・構造・床面積）を処理する
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from touki_parser import parse_hyodai_tatemono
from .base_agent import BaseAgent


class TatemonoAgent(BaseAgent):
    doc_type = "tatemono"

    def parse_hyodai(self, hyodai: str, full_text: str) -> dict:
        return parse_hyodai_tatemono(hyodai)
