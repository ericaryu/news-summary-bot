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
    ("The Verge AI",             "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),  # ✅ AI 전용 피드로 교체

    # ── 일본 미디어 (AI 전용 피드 없음 → 키워드 필터 적용) ──────────
    ("Qiita AI",                 "https://qiita.com/tags/ai/feed"),                # ✅ AI 태그 피드
    ("PR Times AI",              "https://prtimes.jp/topics/keywords/AI/feed"),    # ✅ AI 토픽 피드
    ("Gigazine",                 "https://gigazine.net/news/rss_2.0/"),            # AI 전용 피드 없음 → 키워드 필터
    ("ASCII.jp",                 "https://ascii.jp/rss.xml"),                      # AI 전용 피드 없음 → 키워드 필터
    ("Nikkei Asia",              "https://asia.nikkei.com/rss/feed/nar"),          # AI 전용 피드 없음 → 키워드 필터
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
    "Qiita AI",
    "PR Times AI",
}


def is_ai_related(title: str, content: str) -> bool:
    text = (title + " " + content).lower()
    return any(kw.lower() in text for kw in AI_KEYWORDS)


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


def summarize(title, content, source):
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": f"""다음 기술 블로그 글을 비개발자도 이해할 수 있게 핵심만 3줄로 요약하고, 비개발직군(사업팀, 기획자, 마케터 등)에게 얼마나 유용한지 평가해줘.
전문 용어는 쉬운 말로 풀어서 설명해줘.

출처: {source}
제목: {title}
내용: {content[:3000]}

아래 형식으로만 답해줘 (다른 말 붙이지 말고):
- 첫 번째 줄
- 두 번째 줄
- 세 번째 줄

[비개발직군 관련도]
등급: 높음 / 보통 / 낮음 중 하나만 선택
한줄: 알면 좋은 이유를 한 줄로

등급 판정 규칙(중요):
- 낮음(약 30%): 비개발직군이 '꼭 읽어보면 좋은' 수준. 실무 직접 적용성은 낮지만 트렌드/배경 이해에 유익.
- 보통(약 30%): 읽으면 업무(기획/마케팅/사업)에 도움될 수도 있는 수준. 간접 적용 가능.
- 높음(약 40%): 개발직군에 더 직접적으로 유용한 기술 구현/아키텍처/성능/코드 중심 내용.
- 전체 기사들을 상대적으로 분류해 비율이 한쪽으로 치우치지 않게 조정.
- 애매하면 '높음'보다 '보통' 또는 '낮음'을 우선 검토.

한줄 작성 규칙(중요):
- 반드시 이 글의 구체적인 내용을 근거로 써야 한다.
- 등급이 '높음'이면: 비개발자가 읽지 않아도 되는 이유를 이 글의 내용 기반으로 솔직하게 써라.
- 등급이 '보통'이면: 이 글의 어떤 내용이, 어떤 직군의, 어떤 실무 상황에 직접 연결되는지 써라.
- 등급이 '낮음'이면: 이 글이 어떤 흐름이나 맥락을 이해하는 데 왜 유익한지 써라.
- 이 글에서 실제로 다루는 기술명, 수치, 상황을 반드시 언급하고 끝내라.
- 글의 내용을 한 번도 언급하지 않은 채 마무리되는 문장은 다시 써라.
- 2~3문장이 되더라도 구체성이 우선이다.""",
            }
        ],
    )
    return response.choices[0].message.content.strip()


def parse_summary(raw):
    if "[비개발직군 관련도]" in raw:
        parts = raw.split("[비개발직군 관련도]", 1)
        summary = parts[0].strip()
        relevance_raw = parts[1].strip()

        grade = ""
        reason = ""
        for line in relevance_raw.splitlines():
            line = line.strip()
            if line.startswith("등급:"):
                grade = line.replace("등급:", "").strip()
            elif line.startswith("한줄:"):
                reason = line.replace("한줄:", "").strip()

        grade_emoji = {"높음": "🔴", "보통": "🟡", "낮음": "⚪"}.get(grade, "❓")
        relevance = f"{grade_emoji} *관련도: {grade}*  |  {reason}" if grade else relevance_raw
        return summary, relevance
    return raw, None


def post_to_slack(source, title, link, summary):
    body, relevance = parse_summary(summary)

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
                "text": f"*💡 3줄 요약*\n{body}",
            },
        },
    ]

    if relevance:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*👥 비개발직군 참고*\n{relevance}",
                },
            }
        )

    blocks.append({"type": "divider"})

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
