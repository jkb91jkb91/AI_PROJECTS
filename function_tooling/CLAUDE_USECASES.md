# Komendy

```

# GET NEWEST DATA FROM CHANNEL
sync_channel(channel_url: https://www.youtube.com/@cezarygraf, limit: 20, tab: videos, display_summaries_in_chat: false)
sync_channel(channel_url: https://www.youtube.com/@cezarygraf, limit: 20, tab: streams, display_summaries_in_chat: false)
sync_channel(channel_url: https://www.youtube.com/@cezarygraf, limit: 5, tab: videos, display_summaries_in_chat: true)



# THIS IS SUMMARY ONLY FROM DATABASE CONTENT, 1 FILE
get_summary(channel_url: https://www.youtube.com/@cezarygraf, comment: "")
get_summary(channel_url: https://www.youtube.com/@cezarygraf, comment: "skup się na prognozach")


# THIS IS SUMMARY FROM N COUNT OF VIDEOS
recent_summaries(channel: https://www.youtube.com/@cezarygraf, limit: 3)
recent_summaries(channel: https://www.youtube.com/@cezarygraf, limit: 0)

search_summaries(query: "podatki", channel: https://www.youtube.com/@cezarygraf, limit: 5, sort: "date")

get_video_summary(video: https://www.youtube.com/watch?v=XXXXXXXXXXX)

index_status(channel: https://www.youtube.com/@cezarygraf)
list_indexed_channels()

list_channel_videos(channel_url: https://www.youtube.com/@cezarygraf, limit: 20, tab: videos)
index_channel(channel_url: https://www.youtube.com/@cezarygraf, limit: 50, tab: videos)
summarize_videos(video_urls: [https://www.youtube.com/watch?v=XXXXXXXXXXX])
get_video_subtitles(video_urls: [https://www.youtube.com/watch?v=XXXXXXXXXXX], lang: pl, max_chars: 20000)
```
