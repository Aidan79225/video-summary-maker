"""領域實體：不依賴任何框架或 IO，純資料與規則。"""
from __future__ import annotations

import math
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


# --- 多人模式（speaker_mode="diarize"）的實體 ---
#
# 單人路徑一個字都不碰這些：Cue、Transcript、既有的 Slide 欄位都和 master 相同。
# 多人模式不走字幕，從語音辨識的「字」開始，經過說話者歸屬、分段（Turn），
# 再由模型只引用 Turn 的編號寫出重點（Point）。說話者的身分永遠由程式從 diarization
# 的結果推出來，模型看不到、也決定不了。


@dataclass(frozen=True)
class SpeakerTurn:
    """diarization 找到的一段聲音與說話者；speaker 是 diarizer 給的原始標籤。"""
    start: float
    end: float
    speaker: str


@dataclass(frozen=True)
class DiarizerOutput:
    """一次說話者區分的結果。

    turns 是互斥的區段（同一時間只有一人），拿來對齊語音辨識的字；overlap_seconds
    是同一時間有兩人以上在說話的總秒數，只用來算 overlap_ratio 給人看。
    """
    turns: tuple[SpeakerTurn, ...]
    overlap_seconds: float = 0.0


@dataclass(frozen=True)
class Word:
    """語音辨識的一個字（Whisper 的 word timestamps）。

    中文多半一個字一個 Word，標點黏在前一個字後面；英文則帶著前導空白。
    segment 是它所屬的 Whisper 段落編號，只用來辨認「同一段被重複輸出」。
    """
    start: float
    end: float
    text: str
    segment: int = 0
    probability: float = 1.0


@dataclass(frozen=True)
class WordTranscript:
    """多人模式的語音辨識結果與影片資訊，整份存進存檔（words 階段）。"""
    video_id: str
    title: str
    duration: float
    language: str
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Turn:
    """同一位說話者連續說的一段話。

    speaker 是 "S1".."Sn"（不透明的代號，依第一次出現排序），"S0" 表示無法判斷是誰。
    index 是它在整份逐字稿裡從 0 開始的位置：模型引用的就是這個數字，
    points[].turn 帶的也是它。
    """
    index: int
    start: float
    end: float
    speaker: str
    text: str


@dataclass(frozen=True)
class SpeakerSample:
    """說話者清單上的一段樣本：給操作者聽聲音、確認是不是同一個人。"""
    start: float
    text: str


@dataclass(frozen=True)
class SpeakerInfo:
    """一位說話者的統計。label 是程式給的中性代號（發言者 A／其他聲音 1），不是真人姓名。

    kind 為 "participant"（主要發言者）或 "minor"（只出現一下的聲音：不進模型的可引用範圍）。
    """
    id: str
    label: str
    kind: str
    seconds: float
    turns: int
    first_seen: float
    last_seen: float
    samples: tuple[SpeakerSample, ...] = ()
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiarizationReport:
    """這次說話者區分的來歷與品質；status 為 "ok" 或 "warn"，warn 時 warnings 說明原因。"""
    engine: str
    version: str
    model: str
    fingerprint: str
    status: str
    warnings: tuple[str, ...]
    requested_speakers: int | None
    found_speakers: int
    minor_speakers: int
    speech_seconds: float
    unassigned_ratio: float
    overlap_ratio: float
    # 模型提出的重點數，與通過程式檢查而留下來的重點數
    proposed_points: int = 0
    kept_points: int = 0
    # 只處理影片的一段區間時，實際處理的起訖（原影片的秒數，終點已夾在影片長度內）；整支影片是 None。
    # 兩個一起有或一起沒有。在 API 契約裡（只在有區間時才輸出，見 slidebox_api/payload.py）。
    range_start: float | None = None
    range_end: float | None = None
    # 下面四個只給記錄與 AttributionUnreliable 的訊息看，**不在 API 契約裡**（payload 不輸出）。
    # None = 這次沒有重新檢查（沿用上次存的對談重點），不是 0。
    # rescued_points：留下的點裡，改用「原話」代替模型改寫的點數（見 usecases/conversation.py 的混合條列）
    rescued_points: int | None = None
    # contradicted_points：程式檢查發現文字有問題而丟掉的點數（數字、問句、複述、姓名、代名詞…）
    contradicted_points: int | None = None
    # unverifiable_points：無法核對出處而丟掉的點數（引文不在任何一輪、內容找不到依據又不能改用原話）
    unverifiable_points: int | None = None
    # anchor_conflicts：其中內容出自 quote 所在那一輪以外的點數（contradicted_points 的一部分）
    anchor_conflicts: int | None = None


@dataclass(frozen=True)
class Point:
    """一個通過檢查的重點。speaker 是程式從 turn 查出來的，不是模型說的。

    text 是不帶主詞的短句（「[[Sn]]：」由程式加上去），evidence 是被引用那段話裡
    最貼近 text 的原文，讓人一眼核對。
    """
    speaker: str
    turn: int
    start: float
    text: str
    evidence: str


@dataclass(frozen=True)
class RawPoint:
    """模型回傳、尚未檢查的重點：只有它引用的 turn 編號，沒有說話者。

    context 是回答很短（對、不是）時，模型補充的上文 turn 編號。
    quote 是模型從逐字稿「照抄」的一小段原文（先抄、再填 turn、最後才寫 text）：程式拿它找出
    真正說這段話的那一輪，說話者就是那一輪的說話者。預設空字串（沒有引文的點無法核對，會被丟掉）。
    """
    turn: int
    text: str
    context: tuple[int, ...] = ()
    quote: str = ""


@dataclass(frozen=True)
class RawSlide:
    """模型回傳、尚未檢查的一頁。"""
    title: str
    points: tuple[RawPoint, ...]


@dataclass(frozen=True)
class AudioClip:
    """下載好的音訊檔與其影片資訊。

    語音路徑是在字幕 gateway 失敗之後才走的，拿不到它原本提供的 metadata，
    所以由音訊 gateway 一併帶回。

    offset：這個檔案是原影片的哪一秒開始切出來的（沒有切過就是 0）。聲音區分與語音辨識
    量到的時間是從檔案開頭算起的，要加上它才是原影片的時間（見 usecases/time_range.py）。
    """
    path: str
    video_id: str
    title: str
    duration: float
    offset: float = 0.0


# 區間至少要這麼長才處理：更短的幾乎一定是打錯，而且這麼短的音訊，聲音區分與語音辨識
# 什麼也做不出來。
MIN_RANGE_SECONDS = 5.0


def _range_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"區間的{name}要是有限的數字（秒），不是 {value!r}")
    return float(value)


@dataclass(frozen=True)
class TimeRange:
    """影片裡要處理的一段連續區間，單位是原影片的秒數。

    start 是 0＝從頭開始；end 是 None＝一直到影片結尾。兩端都沒給的區間就是整支影片
    （usecases.time_range.normalize_range 會把它當成沒有區間）。
    """
    start: float = 0.0
    end: float | None = None

    def __post_init__(self) -> None:
        start = _range_number(self.start, "起點")
        if start < 0:
            raise ValueError(f"區間的起點不能是負的：{start:g}")
        if self.end is not None:
            end = _range_number(self.end, "終點")
            if end - start < MIN_RANGE_SECONDS:
                raise ValueError(
                    f"區間的終點（{end:g}）要比起點（{start:g}）晚至少 {MIN_RANGE_SECONDS:g} 秒")


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
    # 多人模式才有：這一頁每個重點由誰說、出自哪一段話。單人頁面維持空 tuple。
    points: tuple[Point, ...] = ()


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
    # 這個數字若是在講某部法律某一條的內容（罰鍰、刑期、期限），記下法律名稱
    # 與條號（正規化成「第N條」），讓新聞服務去取條文原文附在旁邊。不是
    # 就兩個都空字串。這裡只記「講的是哪一條」，不判斷講得對不對。
    law: str = ""
    article: str = ""


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
    # 多人模式（"diarize"）才會改；議會與單人影片維持 "single"，其餘兩個欄位也維持空。
    speaker_mode: str = "single"
    speakers: tuple[SpeakerInfo, ...] = ()
    diarization: DiarizationReport | None = None

    @property
    def missing_images(self) -> int:
        return sum(1 for s in self.slides if s.image_path is None)


@dataclass(frozen=True)
class TopicLabel:
    """一個可選的政策領域。

    清單由呼叫端（新聞服務）每次帶來，這邊不寫死：領域只有新聞服務那一份，
    兩邊各寫一份遲早會對不上。key 給資料庫與網址用；label 是名稱，給模型
    看——「衛生福利」本身就帶著意思，welfare 只是多一層要模型猜的對照。
    description 補充這個名稱涵蓋哪些事。
    """
    key: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class TopicResult:
    """一段文字的分類結果。primary／secondary 是 key，不是名稱。"""
    primary: str
    # 沒有占相當篇幅的第二個領域就是 None
    secondary: str | None
    # 「模型#提示詞版本#提示指紋」：新聞服務只採用評估通過的那個版本分出來的
    # 結果，所以每一筆都要帶著自己是誰分的。
    classifier: str


@dataclass(frozen=True)
class FollowUpPair:
    """要判斷的一對：先前的一項要求，與同一個人後來的一篇發言。

    內容全由呼叫端（新聞服務）挑好帶來：哪篇算候選、逐字稿取哪一段，都要看
    新聞服務的資料庫，GPU 端只負責判斷這一對。
    """
    # 先前摘要卡 asks 裡的那一項要求
    request: str
    # 官員當時的回應；當場沒有回應就是空字串
    response: str
    # 後來那篇的摘要卡：一句話、它自己的要求、各段小標
    card: str
    # 後來那篇逐字稿裡跟要求最相關的一段；引用只能從這裡抄
    excerpt: str


@dataclass(frozen=True)
class FollowUpResult:
    """一對的判斷結果。"""
    followed_up: bool
    # 從 excerpt 抄出來的證據；沒有追問就是空字串。是不是真的在逐字稿裡由
    # 新聞服務檢查（落地檢查），這邊不改判。
    quote: str
    # 「模型#提示詞版本#提示指紋」：同 TopicResult，新聞服務只採用評估通過的
    # 那個判斷器判出來的結果。
    classifier: str


@dataclass(frozen=True)
class DeckResult:
    """use case 的回傳：成品與它的落點。UI 需要路徑才能提供「開啟」。"""
    deck: Deck
    html_path: str
    # 第一張沒截到的圖為什麼失敗（下載片段或抽幀）；都有圖時是空字串。只給畫面看、不寫進 HTML：
    # 那是給跑的人查問題用的，不是給讀成品的人看的
    screenshot_problem: str = ""

    @property
    def missing_images_note(self) -> str:
        """「，其中 N 頁沒有截圖（原因）」；都有圖時是空字串。use case 的最後一則狀態與畫面共用這一句。"""
        missing = self.deck.missing_images
        if not missing:
            return ""
        reason = f"（{self.screenshot_problem}）" if self.screenshot_problem else ""
        return f"，其中 {missing} 頁沒有截圖{reason}"


class SectionClips(list):
    """VideoSectionGateway.download_sections 的回傳：每個時間點一個片段路徑（失敗為 None），
    另外帶著第一個失敗原因。

    進度訊息裡的原因幾毫秒就被後面的狀態蓋掉，要留到完成訊息就得交給 use case。是 list 的
    子類別，依位置取、len、跟 list 比較都照舊，只回傳 list 的 gateway 也還能用（沒有原因而已）。
    """

    def __init__(self, paths=(), failure: str = ""):
        super().__init__(paths)
        self.failure = failure


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
