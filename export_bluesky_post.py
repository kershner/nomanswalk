import os, re, sys, subprocess, requests
from nms_bluesky import _load_params

API = "https://public.api.bsky.app/xrpc"

# small helper for Bluesky public API calls
def get_json(endpoint, **params):
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def export_post(arg=None, out="bsky_export"):
    # load handle and resolve to DID (more reliable for URIs)
    handle = _load_params()["BLUESKY_HANDLE"]
    did = get_json("com.atproto.identity.resolveHandle", handle=handle)["did"]

    # resolve input → at:// URI (latest post, URL, full URI, or rkey)
    if not arg:
        uri = next(p["post"]["uri"] for p in get_json(
            "app.bsky.feed.getAuthorFeed", actor=handle, limit=5
        )["feed"])
    elif arg.startswith("http"):
        m = re.search(r"/profile/([^/]+)/post/([^/?#]+)", arg)
        uri = f"at://{m.group(1)}/app.bsky.feed.post/{m.group(2)}"
    elif arg.startswith("at://"):
        uri = arg
    else:
        uri = f"at://{did}/app.bsky.feed.post/{arg}"

    # fetch post data
    posts = get_json("app.bsky.feed.getPosts", uris=uri)["posts"]
    if not posts:
        raise Exception(f"Post not found: {uri}")
    post = posts[0]

    # extract video embed (handle repost/media wrapper case)
    embed = post.get("embed") or {}
    if embed.get("$type") == "app.bsky.embed.recordWithMedia#view":
        embed = embed.get("media") or {}

    # HLS playlist URL for the video
    playlist = embed.get("playlist")
    if not playlist:
        raise Exception("No video")

    # create output folder per post
    d = os.path.join(out, uri.rsplit("/", 1)[-1])
    os.makedirs(d, exist_ok=True)

    # ffmpeg pipeline:
    #  - background: scaled to fill 9:16, cropped, blurred
    #  - foreground: scaled to fit inside 9:16
    #  - overlay centered → TikTok-style vertical video
    subprocess.run([
        "ffmpeg", "-y", "-i", playlist,
        "-filter_complex",
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:8[bg];"
        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]",
        "-map", "[v]",
        "-map", "0:a?",  # include audio if present
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        os.path.join(d, "video.mp4"),
    ], check=True)

    # save caption text
    open(os.path.join(d, "caption.txt"), "w", encoding="utf-8").write(
        (post["record"].get("text") or "").strip()
    )

    print(d)

if __name__ == "__main__":
    export_post(sys.argv[1] if len(sys.argv) > 1 else None)