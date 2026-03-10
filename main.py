import feedparser
import requests
import json
import os
import re
import hashlib
from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

RSS_FEEDS = [
    ("MUSINSA tech",             "https://medium.com/feed/musinsa-tech"),
    ("올리브영 테크블로그",          "https://oliveyoung.tech/rss.xml"),
    ("토스테크",                   "https://toss.tech/rss.xml"),
    ("D2 Blog (Naver)",          "https://d2.naver.com/d2.atom"),
    ("우아한형제들",                "https://techblog.woowahan.com/feed/"),
    ("컬리 기술 블로그",            "https://helloworld.kurly.com/feed.xml"),
    ("카카오엔터프라이즈",           "https://tech.kakaoenterprise.com/rss"),
    ("LY Corp Tech - AI",        "https://techblog.lycorp.co.jp/ko/tag/AI/feed/index.xml"),
    ("TechCrunch AI",            "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("VentureBeat AI",           "https://venturebeat.com/category/ai/feed/"),
    ("OpenAI News",              "https://openai.com/news/rss.xml"),
]

SEEN_FILE = "seen_articles.json"


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f, indent=2)


def get_article_id(entry):
    return (
        entry.get("id")
        or entry.get("link")
        or hashlib.md5(entry.get("title", "").encode()).hexdigest()
    )


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
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": f"""다음 기술 블로그 글을 비개발자도 이해할 수 있게 핵심만 3줄로 요약하고, 비개발직군(사업팀, 기획자, 마케터 등)에게 얼마나 유용한지 평가해줘.
전문 용어는 쉬운 말로 풀어서 설명해줘.

출처: {source}
제목: {title}
내용: {content[:3000]}

아래 형식으로만 답해줘 (다른 말 붙이지 말고):
• 첫 번째 줄
• 두 번째 줄
• 세 번째 줄

[비개발직군 관련도]
등급: 높음 / 보통 / 낮음 중 하나만 선택
한줄: 알면 좋은 이유를 한 줄로

등급 판정 규칙(중요):
- 낮음(약 30%): 비개발직군이 '꼭 읽어보면 좋은' 수준. 실무 직접 적용성은 낮지만 트렌드/배경 이해에 유익.
- 보통(약 30%): 읽으면 업무(기획/마케팅/사업)에 도움될 수도 있는 수준. 간접 적용 가능.
- 높음(약 40%): 개발직군에 더 직접적으로 유용한 기술 구현/아키텍처/성능/코드 중심 내용.
- 전체 기사들을 상대적으로 분류해 비율이 한쪽으로 치우치지 않게 조정.
- 애매하면 '높음'보다 '보통' 또는 '낮음'을 우선 검토.""",
            }
        ],
    )
    return response.choices[0].message.content.strip()


def parse_summary(raw):
    """3줄 요약과 비개발직군 관련도 섹션을 분리해서 반환."""
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

    for source, feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                article_id = get_article_id(entry)
                if article_id in seen:
                    continue

                title = entry.get("title", "제목 없음")
                link = entry.get("link", "")
                content = get_content(entry)

                print(f"New: [{source}] {title}")
                summary = summarize(title, content, source)
                post_to_slack(source, title, link, summary)

                seen.add(article_id)
                new_count += 1

        except Exception as e:
            print(f"Error [{source}]: {e}")

    if new_count > 0:
        save_seen(seen)
        print(f"Done: {new_count} articles posted")
    else:
        print("No new articles")


if __name__ == "__main__":
    main()
