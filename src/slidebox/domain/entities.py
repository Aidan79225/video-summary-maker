"""領域實體：不依賴任何框架或 IO，純資料與規則。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    """字幕的一句。"""
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    video_id: str
    title: str
    duration: float
    cues: tuple[Cue, ...]
    language: str
    # 是否為 YouTube 的滾動式自動字幕：決定是否做滾動去重。
    # 語音辨識的結果不是滾動字幕，設 False——套用滾動去重只會誤刪內容。
    is_automatic: bool = False
    # 來源自己知道的品質註記，例如立法院的 AI 逐字稿。is_automatic 講的是
    # 「滾動字幕」這個技術事實，不是品質標籤：有些來源兩者不重合，所以讓
    # 來源自己說。空字串表示由 use case 依 is_automatic 判斷。
    source_note: str = ""


@dataclass(frozen=True)
class AudioClip:
    """下載好的音訊檔與其影片資訊。

    語音路徑是在字幕 gateway 失敗之後才走的，拿不到它原本提供的 metadata，
    所以由音訊 gateway 一併帶回。
    """
    path: str
    video_id: str
    title: str
    duration: float


@dataclass(frozen=True)
class Slide:
    """一頁投影片。image_path 由抽幀階段後填，摘要階段一律為 None。"""
    index: int
    title: str
    bullets: tuple[str, ...]
    timestamp: float
    image_path: str | None = None
    # 詳細模式下該章節的完整敘述；一般模式為空字串。條列是「提醒你看過什麼」，
    # 這一段是「沒看影片也能懂」，兩者用途不同，不能互相取代。
    detail: str = ""


@dataclass(frozen=True)
class KeyNumber:
    """質詢卡上的一個關鍵數字。

    value 只放阿拉伯數字（「82.4」），單位另放，卡片才能把數字放大、單位
    縮小。quote 是逐字稿裡講出這個數字的那句話——它是驗證的依據：句子不在
    逐字稿裡、或數字不在句子裡，這個數字就不上卡片。
    """
    value: str
    unit: str
    label: str
    quote: str


@dataclass(frozen=True)
class Ask:
    """講者提出的一項要求，以及對方當場的回應。"""
    request: str
    deadline: str = ""
    response: str = ""


@dataclass(frozen=True)
class Brief:
    """質詢卡：讀者十秒內要知道的事。

    分段摘要是「照影片順序講了什麼」，質詢卡是「他到底要什麼、拿到什麼
    回應、憑什麼數字」。兩者用途不同：前者是散文，後者是固定欄位，讀者
    知道眼睛該往哪看。
    """
    one_liner: str
    key_numbers: tuple[KeyNumber, ...] = ()
    asks: tuple[Ask, ...] = ()


@dataclass(frozen=True)
class Deck:
    source_url: str
    video_title: str
    slides: tuple[Slide, ...]
    # 內容來源的註記，例如「由語音辨識產生」。寫在成品裡而不只是狀態列：
    # 狀態列幾毫秒後就被蓋掉，而 HTML 會被分享、會被日後重看。
    source_note: str = ""
    # 詳細模式下附在成品末尾的完整逐字稿；一般模式為空字串。
    transcript_text: str = ""
    # 詳細模式下另外產生的質詢卡；產不出來時為 None，成品照常出片。
    brief: Brief | None = None

    @property
    def missing_images(self) -> int:
        return sum(1 for s in self.slides if s.image_path is None)


@dataclass(frozen=True)
class DeckResult:
    """use case 的回傳：成品與它的落點。UI 需要路徑才能提供「開啟」。"""
    deck: Deck
    html_path: str


@dataclass
class Settings:
    """使用者設定，持久化於 slidebox_settings.json。"""
    output_dir: str
    model: str = "qwen3.5:9b"
    ollama_host: str = "http://localhost:11434"
    max_height: int | None = 1080
    min_slides: int = 8
    max_slides: int = 15
    image_width: int = 1280
    num_ctx: int = 32768
    char_budget: int = 20000
    # 簡中排在繁中之後、英文之前：輸出是繁體，但人工簡中已經是中文，比英文好。
    subtitle_langs: tuple[str, ...] = (
        "zh-TW", "zh-Hant", "zh-HK", "zh", "zh-Hans", "zh-CN", "en",
    )
    # 沒有字幕時的語音辨識模型。large-v3-turbo 在 CPU 上比 medium 還快、品質
    # 最佳；實測日韓 3.9～4.9 倍即時。改 small 約快兩成，但日文明顯較差。
    whisper_model: str = "large-v3-turbo"
    # 詳細模式：每頁多產一段完整敘述，並在 HTML 末尾附上完整逐字稿。
    # 給「不想看影片但要知道全部內容」的情況，代價是生成較慢、檔案較大。
    detailed: bool = False
