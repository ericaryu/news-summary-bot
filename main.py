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
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": f"""다음 기술 블로그 글을 비개발자도 이해할 수 있게 핵심만 3줄로 요약해줘.
전문 용어는 쉬운 말로 풀어서 설명해줘.

출처: {source}
제목: {title}
내용: {content[:3000]}

아래 형식으로만 답해줘 (다른 말 붙이지 말고):
• 첫 번째 줄
• 두 번째 줄
• 세 번째 줄""",
            }
        ],
    )
    return response.choices[0].message.content.strip()


def post_to_slack(source, title, link, summary):
    payload = {
        "blocks": [
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
                    "text": f"*💡 3줄 요약*\n{summary}",
                },
            },
            {"type": "divider"},
        ]
    }
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
