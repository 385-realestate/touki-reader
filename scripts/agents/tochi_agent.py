"""
土地エージェント
表題部の土地固有フィールド（所在・地番・地目・地積）を処理する
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from touki_parser import parse_hyodai_tochi
from .base_agent import BaseAgent


class TochiAgent(BaseAgent):
    doc_type = "tochi"

    def parse_hyodai(self, hyodai: str, full_text: str) -> dict:
        return parse_hyodai_tochi(hyodai)
