/**
 * 議題分布的固定列舉與委員會職掌對照。
 *
 * 唯一的來源在後端（services/news/articles/topics.py）：側寫的分布、篩選都以後端
 * 回的為準。網站留一份是因為方法頁要把這兩張表公開給讀者看，而發言者頁的
 * 「議題：○○」篩選標籤在側寫沒載入時也要寫得出名稱。後端改了列舉或對照，
 * 這裡跟方法頁要一起改。
 */

export type TopicArea = {
  /** 網址與資料庫用的代碼 */
  key: string;
  /** 給讀者看的名稱，也是給模型看的選項 */
  label: string;
  /** 給模型的說明：這個領域包含哪些事 */
  description: string;
};

/** 順序就是後端的順序：分布同篇數時依這個順序排，方法頁也照這個順序列 */
export const TOPIC_AREAS: readonly TopicArea[] = [
  { key: 'defense', label: '國防外交', description: '國防、軍事、外交、兩岸、僑務' },
  { key: 'finance', label: '財政經濟', description: '預算、稅收、金融、產業、經濟政策、物價' },
  { key: 'interior', label: '內政治安', description: '警政、治安、消防、移民、戶政、選務' },
  { key: 'education', label: '教育文化', description: '教育、學校、文化、體育' },
  { key: 'welfare', label: '衛生福利', description: '醫療、健保、長照、社福、托育、食安' },
  { key: 'transport', label: '交通建設', description: '交通、道路、捷運、公共工程、觀光' },
  { key: 'environment', label: '環境能源', description: '環保、污染、氣候、能源、電力、水資源' },
  { key: 'justice', label: '司法法制', description: '司法、檢調、監所、法律制度' },
  { key: 'agriculture', label: '農業', description: '農、漁、牧、農產品、農村' },
  { key: 'labor', label: '勞動', description: '勞工、就業、薪資、勞保與年金' },
  { key: 'digital', label: '數位科技', description: '數位發展、資安、通訊、科技研發' },
  { key: 'local', label: '地方建設／其他', description: '都市計畫、住宅、區里建設、議事程序、以上都不是的' },
];

/** 立法院各委員會職掌內的領域（委員會職掌內的比例用這張表） */
export const COMMITTEE_AREAS: readonly { committee: string; areas: readonly string[] }[] = [
  { committee: '內政委員會', areas: ['interior'] },
  { committee: '外交及國防委員會', areas: ['defense'] },
  { committee: '經濟委員會', areas: ['finance', 'agriculture', 'environment'] },
  { committee: '財政委員會', areas: ['finance'] },
  { committee: '教育及文化委員會', areas: ['education', 'digital'] },
  { committee: '交通委員會', areas: ['transport', 'digital'] },
  { committee: '司法及法制委員會', areas: ['justice'] },
  { committee: '社會福利及衛生環境委員會', areas: ['welfare', 'labor', 'environment'] },
];

/**
 * 評估通過的門檻（後端的 TOPIC_MIN_LABELS、TOPIC_MIN_ACCURACY）。
 *
 * 後端沒通過就不給議題區塊；網站再擋一次，理由跟同儕人數的 MIN_PEERS 一樣：
 * 「準確率不到門檻不上線」是這一塊的底線，不該只靠一邊守。後端改門檻時這裡要跟著改。
 */
export const TOPIC_MIN_LABELS = 20;
export const TOPIC_MIN_ACCURACY = 0.8;

/** 廣度的門檻：占比至少這麼多的領域才算一個（後端同一個數字） */
export const TOPIC_BREADTH_SHARE = 10;

const BY_KEY = new Map(TOPIC_AREAS.map((t) => [t.key, t]));

/** 網址上的 ?topic= 只認列舉裡的代碼：亂打的當成沒篩，不把後端的 422 傳染給整頁 */
export function isTopicKey(key: string): boolean {
  return BY_KEY.has(key);
}

export function topicLabel(key: string): string {
  return BY_KEY.get(key)?.label ?? key;
}
