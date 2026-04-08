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
    ("Qiita AI",                 "https://qiita.com/tags/ai/feed"),
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
    "Qiita AI",
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


# ─────────────────────────────────────────────────────────────────
#  개선된 프롬프트
# ─────────────────────────────────────────────────────────────────

SUMMARY_PROMPT = """다음 기술 블로그/뉴스 글을 읽고 두 가지를 해줘.

━━━ 1. 요약 ━━━
비개발자(사업팀, 기획자, 마케터)가 이 글의 핵심을 파악할 수 있게 요약해줘.
- 분량: 2~5줄. 핵심이 2줄이면 2줄, 설명이 필요하면 5줄까지 가능.
- 전문 용어가 나오면 괄호 안에 쉬운 말로 풀어줘.
- "~라고 한다", "~할 수 있다" 같은 애매한 마무리 대신, 글이 실제로 말하는 팩트를 써줘.
- 글에 수치, 사례, 비교가 있으면 반드시 포함해줘.

━━━ 2. 비개발직군 판정 ━━━
이 글을 비개발직군이 읽어야 하는지 판정해줘.

등급은 세 가지:
📌 필독 — 비개발직군 실무에 바로 쓸 수 있는 내용 (AI 도구 활용법, 업무 자동화 사례, 시장/산업 트렌드, 비즈니스 전략 등)
📎 참고 — 직접 쓰진 못하지만 트렌드나 맥락 이해에 도움 (새 기술 발표, 업계 동향, 경쟁사 움직임 등)
⏭️ 스킵 — 개발자/엔지니어 대상 기술 구현 내용 (코드 아키텍처, 성능 최적화, 인프라 설계, 라이브러리 사용법 등)

판정 규칙:
- 제목이나 주제가 그럴듯해 보여도, 본문이 코드/설정/아키텍처 중심이면 ⏭️ 스킵이다.
- "이 개념을 알면 마케팅에도 도움이 될 수 있다" 같은 억지 연결을 하지 마라. 실제로 비개발직군이 내일 당장 업무에 쓸 수 있는지만 봐라.
- 애매하면 ⏭️ 스킵을 줘라. 관대하게 주지 마라.

한줄 코멘트 규칙:
- 📌 필독: 어떤 직군이, 어떤 상황에서, 이 글의 어떤 내용을 쓸 수 있는지 구체적으로.
- 📎 참고: 이 글이 어떤 흐름/맥락을 이해하는 데 왜 유용한지, 글의 구체적 내용을 근거로.
- ⏭️ 스킵: 이 글이 다루는 기술 주제를 한 문장으로 정리. (비개발자가 제목만 보고 "아 이런 글이구나" 판단할 수 있게)

━━━ 예시 ━━━

예시 A) 글 제목: "CLAUDE.md 최적화 — LLM Attention 메커니즘 역산 설계"
→ 본문이 LLM 설정 파일의 토큰 배치, Recency Bias 활용, 프롬프트 구조 최적화를 다룸

요약:
LLM에게 지시를 내리는 설정 파일(CLAUDE.md)을 효과적으로 작성하는 방법을 다룬다. 100줄짜리 설정보다 35줄짜리가 더 효과적이라는 실험 결과를 근거로, LLM이 텍스트를 읽는 방식(최근 내용에 더 집중하는 Recency Bias)을 역이용한 구조 설계법을 제안한다.

[비개발직군 판정]
⏭️ 스킵 | AI 코딩 도구의 설정 파일 작성법을 다루는 개발자 대상 글이다.

예시 B) 글 제목: "생성AI로 고객 문의 자동 분류 — 도입 3개월 후기"
→ 본문이 CS팀의 문의 분류 자동화 도입 과정, 정확도 92%, 처리시간 60% 단축 사례를 다룸

요약:
CS팀에 생성AI 기반 고객 문의 자동 분류 시스템을 도입한 3개월 후기다. GPT-4o를 활용해 문의를 7개 카테고리로 자동 분류하며, 정확도 92%, 1건당 처리시간이 평균 3분에서 1.2분으로 60% 단축됐다. 오분류 대응을 위해 신뢰도 80% 미만 건은 사람이 재확인하는 하이브리드 방식을 채택했다.

[비개발직군 판정]
📌 필독 | CS·운영팀이 AI 문의 분류 도입을 검토할 때 정확도(92%), 처리시간 단축(60%), 하이브리드 운영 방식을 직접 참고할 수 있다.

━━━ 응답 형식 ━━━
아래 형식으로만 답해. 다른 말 붙이지 마.

요약:
(여기에 요약)

[비개발직군 판정]
등급이모지 등급명 | 한줄 코멘트

출처: {source}
제목: {title}
내용: {content}"""


def summarize(title, content, source):
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=800,
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


def parse_summary(raw):
    """
    파싱 대상 형식:
        요약:
        (본문)

        [비개발직군 판정]
        ⏭️ 스킵 | 한줄 코멘트
    """
    summary = raw
    relevance = None

    if "[비개발직군 판정]" in raw:
        parts = raw.split("[비개발직군 판정]", 1)
        summary = parts[0].strip()
        relevance_line = parts[1].strip().splitlines()[0].strip()

        # "요약:" 접두어 제거
        if summary.startswith("요약:"):
            summary = summary[len("요약:"):].strip()

        relevance = relevance_line  # 이모지+등급+코멘트가 한 줄로 들어옴

    return summary, relevance


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
                "text": f"*💡 요약*\n{body}",
            },
        },
    ]

    if relevance:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*👥 비개발직군*\n{relevance}",
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
