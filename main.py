import feedparser
import requests
import json
import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

RSS_FEEDS = [
    # ── 한국 테크블로그 (키워드 필터 없음) ─────────────────────────
    ("MUSINSA tech",             "https://medium.com/feed/musinsa-tech"),
    ("올리브영 테크블로그",          "https://oliveyoung.tech/rss.xml"),
    ("토스테크",                   "https://toss.tech/rss.xml"),
    ("D2 Blog (Naver)",          "https://d2.naver.com/d2.atom"),
    ("우아한형제들",                "https://techblog.woowahan.com/feed/"),
    ("컬리 기술 블로그",            "https://helloworld.kurly.com/feed.xml"),
    ("카카오엔터프라이즈",           "https://tech.kakaoenterprise.com/rss"),
    ("LY Corp Tech - AI",        "https://techblog.lycorp.co.jp/ko/tag/AI/feed/index.xml"),

    # ── 글로벌 AI 전용 피드 (키워드 필터 없음) ──────────────────────
    ("TechCrunch AI",            "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("VentureBeat AI",           "https://venturebeat.com/category/ai/feed/"),
    ("OpenAI News",              "https://openai.com/news/rss.xml"),
    ("The Verge AI",             "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),

    # ── 일본 미디어 (AI 전용 피드 없음 → 키워드 필터 적용) ──────────
    ("PR Times AI",              "https://prtimes.jp/topics/keywords/AI/feed"),
    ("Gigazine",                 "https://gigazine.net/news/rss_2.0/"),
    ("ASCII.jp",                 "https://ascii.jp/rss.xml"),
    ("Nikkei Asia",              "https://asia.nikkei.com/rss/feed/nar"),
]

AI_KEYWORDS = [
    # 영어
    "ai", "artificial intelligence", "machine learning", "deep learning",
    "llm", "large language model", "generative ai", "gpt", "claude",
    "gemini", "copilot", "chatgpt", "stable diffusion", "diffusion model",
    "neural network", "transformer", "fine-tun", "rag", "vector",
    "embedding", "agent", "automation", "computer vision", "nlp",
    "natural language", "openai", "anthropic", "mistral", "hugging face",
    # 한국어
    "인공지능", "머신러닝", "딥러닝", "생성형", "자동화", "언어모델",
    "챗봇", "AI", "데이터 분석", "자연어",
    # 일본어
    "人工知能", "機械学習", "深層学習", "生成AI", "自動化", "言語モデル",
    "チャットボット", "ディープラーニング", "ベクトル", "エージェント",
]

SKIP_FILTER_SOURCES = {
    # 한국 테크블로그
    "MUSINSA tech",
    "올리브영 테크블로그",
    "토스테크",
    "D2 Blog (Naver)",
    "우아한형제들",
    "컬리 기술 블로그",
    "카카오엔터프라이즈",
    # 글로벌 AI 전용 피드
    "TechCrunch AI",
    "MIT Technology Review AI",
    "VentureBeat AI",
    "OpenAI News",
    "The Verge AI",
    "LY Corp Tech - AI",
    "PR Times AI",
}


def is_ai_related(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    if re.search(r'\bai\b', text):
        return True
    other_keywords = [kw for kw in AI_KEYWORDS if kw != "ai"]
    return any(kw.lower() in text for kw in other_keywords)


SEEN_FILE = "seen_articles.json"
SEEN_TTL_DAYS = 14
MAX_ARTICLE_AGE_DAYS = 7


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            now = datetime.now(timezone.utc).isoformat()
            return {item: now for item in data}
        return data
    return {}


def save_seen(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
    cleaned = {
        k: v for k, v in seen.items()
        if datetime.fromisoformat(v) > cutoff
    }
    with open(SEEN_FILE, "w") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    print(f"seen_articles: {len(cleaned)}개 유지 ({len(seen) - len(cleaned)}개 만료 삭제)")


def get_article_id(entry):
    return (
        entry.get("id")
        or entry.get("link")
        or hashlib.md5(entry.get("title", "").encode()).hexdigest()
    )


def get_entry_published(entry):
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def get_content(entry):
    if entry.get("content"):
        return strip_html(entry.content[0].value)
    return strip_html(entry.get("summary", ""))


SUMMARY_PROMPT = """다음 기술 블로그/뉴스 글의 핵심을 2~5줄로 요약해줘.

규칙:
- 각 줄은 "- "로 시작해줘.
- 핵심이 2줄이면 2줄, 설명이 필요하면 5줄까지 가능.
- 전문 용어가 나오면 괄호 안에 쉬운 말로 풀어줘.
- "~라고 한다", "~할 수 있다" 같은 애매한 마무리 대신 글이 실제로 말하는 팩트를 써줘.
- 수치, 사례, 비교가 있으면 반드시 포함해줘.
- 다른 말 붙이지 말고 불릿 줄만 답해줘.

출처: {source}
제목: {title}
내용: {content}"""


def summarize(title, content, source):
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": SUMMARY_PROMPT.format(
                    source=source,
                    title=title,
                    content=content[:3000],
                ),
            }
        ],
    )
    return response.choices[0].message.content.strip()


def post_to_slack(source, title, link, summary):
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📰 *[{source}]*\n<{link}|{title}>",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": summary,
            },
        },
        {"type": "divider"},
    ]

    payload = {"blocks": blocks}
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload)
    resp.raise_for_status()


def main():
    seen = load_seen()
    new_count = 0
    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)

    for source, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)

            if not feed.entries:
                reason = str(feed.get("bozo_exception", "entries 없음"))
                print(f"Feed 불량 [{source}]: {reason}")
                continue

            skipped_seen = 0
            skipped_old = 0
            skipped_filter = 0
            processed = 0

            for entry in feed.entries[:10]:
                article_id = get_article_id(entry)

                if article_id in seen:
                    skipped_seen += 1
                    continue

                published = get_entry_published(entry)
                if published is not None and published < age_cutoff:
                    skipped_old += 1
                    continue

                title = entry.get("title", "제목 없음")
                content = get_content(entry)

                if source not in SKIP_FILTER_SOURCES:
                    if not is_ai_related(title, content):
                        skipped_filter += 1
                        continue

                link = entry.get("link", "")

                print(f"New: [{source}] {title}")
                summary = summarize(title, content, source)
                post_to_slack(source, title, link, summary)

                seen[article_id] = datetime.now(timezone.utc).isoformat()
                new_count += 1
                processed += 1

            print(
                f"[{source}] 처리={processed}, 중복={skipped_seen}, "
                f"오래된것={skipped_old}, AI무관={skipped_filter}, "
                f"피드총={len(feed.entries)}개"
            )

        except Exception as e:
            print(f"Error [{source}]: {e}")

    save_seen(seen)
    print(f"완료: 총 {new_count}개 기사 발행")


if __name__ == "__main__":
    main()
