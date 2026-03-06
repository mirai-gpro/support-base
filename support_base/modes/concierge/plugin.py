"""
コンシェルジュ モードプラグイン

concierge_{lang}.txt プロンプトを使用し、
長期記憶ベースのパーソナライズ挨拶を提供する。

プロンプトソース:
  - ローカル: prompts/concierge_{lang}.txt
  - フォールバック: ハードコード
"""

import logging
import os

from support_base.modes.gourmet.plugin import GourmetModePlugin

logger = logging.getLogger(__name__)

# 長期記憶の利用可否
try:
    from support_base.core.long_term_memory import LongTermMemory
    LONG_TERM_MEMORY_ENABLED = True
except Exception:
    LONG_TERM_MEMORY_ENABLED = False

# concierge 用の挨拶
try:
    from support_base.core.support_core import INITIAL_GREETINGS as LOADED_GREETINGS
except Exception:
    LOADED_GREETINGS = {}


class ConciergeModePlugin(GourmetModePlugin):
    """
    コンシェルジュモード

    GourmetModePlugin を継承し、以下を変更:
    - name = "concierge"
    - プロンプトファイル: concierge_{lang}.txt
    - 長期記憶を活用したパーソナライズ挨拶
    """

    @property
    def name(self) -> str:
        return "concierge"

    @property
    def display_name(self) -> str:
        return "コンシェルジュ"

    def get_system_prompt(self, language: str = "ja", context: str | None = None) -> str:
        """
        Live API 用システムプロンプト。

        優先順位:
          1. ローカル prompts/concierge_{lang}.txt
          2. ハードコードのフォールバック
        """
        prompt = ""
        source = "none"

        # ローカルファイルを直接読み込み
        prompt_file = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "prompts", f"concierge_{language}.txt"
        )
        prompt_file = os.path.normpath(prompt_file)
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read().strip()
            if prompt and not prompt.startswith("エラー:"):
                source = f"local:{prompt_file}"
        except FileNotFoundError:
            logger.warning(f"[ConciergePlugin] ローカルプロンプト未発見: {prompt_file}")
        except Exception as e:
            logger.warning(f"[ConciergePlugin] ローカルプロンプト読み込み失敗: {e}")

        # フォールバック
        if not prompt or prompt.startswith("エラー:"):
            logger.warning("[ConciergePlugin] ローカルプロンプト使用不可 → フォールバック使用")
            prompt = self._fallback_prompt(language)
            source = "fallback"

        logger.info(
            f"[ConciergePlugin] system_prompt source={source}, "
            f"lang={language}, len={len(prompt)}, "
            f"preview={prompt[:120]!r}"
        )

        # 再接続コンテキスト追加
        if context:
            prompt += f"\n\n【これまでの会話の要約】\n{context}\n"
            prompt += (
                "\n【重要：必ず守ること】\n"
                "1. 直前の話者の発言に対して短い相槌を入れる\n"
                "2. 既に聞いた質問は絶対に繰り返さない\n"
                "3. 会話の流れを自然に引き継ぐ\n"
            )

        return prompt

    def get_initial_greeting(self, language: str = "ja", user_profile: dict | None = None) -> str:
        """
        初回挨拶。長期記憶のユーザープロファイルがあればパーソナライズ。
        """
        # support_core.py の INITIAL_GREETINGS['concierge'] を使用
        concierge_greetings = LOADED_GREETINGS.get("concierge", {})
        base_greeting = concierge_greetings.get(language)

        if not base_greeting:
            fallback = {
                "ja": "いらっしゃいませ。グルメコンシェルジュです。今日はどのようなシーンでお店をお探しでしょうか？",
                "en": "Welcome! I'm your gourmet concierge. What kind of dining experience are you looking for today?",
                "ko": "어서오세요! 저는 귀하의 미식 컨시어지입니다. 오늘은 어떤 식사 장면을 찾으시나요?",
                "zh": "欢迎光临！我是您的美食礼宾员。今天您想找什么样的用餐场景？",
            }
            base_greeting = fallback.get(language, fallback["ja"])

        return base_greeting
