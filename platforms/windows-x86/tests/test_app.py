import json
import struct
import unittest
import uuid
import io
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from xdownloader.account import AccountStore
from xdownloader.core import (
    DownloadError, Media, Tweet, filename, make_session, media_from_entity,
    normalize_url, parse_input, parse_page, read_txt, safe_component, validate_media_url,
)
from xdownloader.engine import Engine, verify_file
from xdownloader.storage import Store

ROOT = Path(__file__).resolve().parents[1]
TARGET = normalize_url("https://x.com/alice/status/123456789")


def folder():
    path = ROOT / "work" / "tests" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def entity(mid="11", kind="animated_gif"):
    return {"id_str": mid, "type": kind, "video_info": {"variants": [
        {"content_type": "video/mp4", "bitrate": 1, "url": f"https://video.twimg.com/tweet_video/{mid}.mp4"}]}}


def tweet_obj(entities=None, tweet_id="123456789"):
    return {"__typename": "Tweet", "rest_id": tweet_id,
            "core": {"user_results": {"result": {"core": {"screen_name": "alice", "name": "爱丽丝"}}}},
            "media_entities2": entities if entities is not None else [entity(), entity("22")]}


def page(obj):
    return '<script>stream.next({data:' + json.dumps(json.dumps(obj)) + '})</script>'


class InputTests(unittest.TestCase):
    def test_normalize_alias_and_query(self):
        self.assertEqual(normalize_url("https://twitter.com/alice/status/123456789/video/2?s=20").url, TARGET.url)

    def test_media_viewer_returns_original_post(self):
        url = "https://x.com/yamada_asmr/status/2097633361682669626/mediaViewer?currentTweet=2097633361682669626&currentTweetUser=yamada_asmr"
        result = normalize_url(url)
        self.assertEqual(result.url, "https://x.com/yamada_asmr/status/2097633361682669626")
        self.assertEqual(normalize_url(url.replace("currentTweet=2097633361682669626", "currentTweet=999999999")).tweet_id, "2097633361682669626")
        self.assertEqual(normalize_url(result.url + "/mediaViewer/#test"), result)

    def test_media_viewer_and_original_deduplicate(self):
        store = Store(folder())
        added, duplicate = store.add_urls([TARGET, normalize_url(TARGET.url + "/mediaViewer?currentTweet=123456789")])
        self.assertEqual((len(added), duplicate), (1, 1))

    def test_unrecognized_path_still_rejected(self):
        for suffix in ("/mediaViewer/extra", "/mediaViewerFake", "/unrelated"):
            with self.assertRaises(DownloadError):
                normalize_url(TARGET.url + suffix)

    def test_reject_foreign_credentials_and_non_https(self):
        for url in ["https://x.com.evil.org/alice/status/123456789", "http://x.com/alice/status/123456789", "https://a:b@x.com/alice/status/123456789", "https://x.com/alice", "https://x.com:80/alice/status/123456789"]:
            with self.subTest(url=url), self.assertRaises(DownloadError):
                normalize_url(url)

    def test_import_reports_every_bad_line(self):
        urls, errors = parse_input(TARGET.url + "\n\nwrong\n" + TARGET.url, "input.txt")
        self.assertEqual(len(urls), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("第 3 行", errors[0])

    def test_concatenated_links_preserve_media_viewer_and_duplicate_detection(self):
        second = 'https://x.com/bob/status/987654321/mediaViewer?currentTweet=987654321&currentTweetUser=bob'
        urls, errors = parse_input(TARGET.url + second + TARGET.url)
        self.assertFalse(errors)
        self.assertEqual([u.tweet_id for u in urls], ['123456789','987654321','123456789'])
        added, duplicate = Store(folder()).add_urls(urls)
        self.assertEqual((len(added),duplicate), (2,1))

    def test_concatenated_txt_keeps_original_error_line_numbers(self):
        path = folder() / 'joined.txt'
        path.write_text(TARGET.url + TARGET.url + '\nwronghttps://evil.org/a\n' + TARGET.url,encoding='utf-8')
        urls, errors = parse_input(read_txt(path), path.name)
        self.assertEqual(len(urls),3)
        self.assertEqual(len(errors),2)
        self.assertTrue(all('第 2 行' in e for e in errors))

    def test_txt_encodings(self):
        path = folder() / "urls.txt"
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            path.write_bytes((TARGET.url + "\n中文").encode(encoding))
            self.assertIn("中文", read_txt(path))

    def test_only_txt(self):
        path = folder() / "urls.csv"
        path.write_text(TARGET.url)
        with self.assertRaises(DownloadError):
            read_txt(path)

    def test_sanitize_filename(self):
        self.assertEqual(safe_component("CON"), "_CON")
        name = filename("alice", '名/字:*?<>|"\\.', 1, "a" * 32, "mp4")
        self.assertTrue(name.endswith(".mp4"))
        self.assertNotIn("/", name)
        self.assertLess(len(filename("u" * 100, "字" * 100, 1, "a" * 32, "mp4")), 130)


class ParserTests(unittest.TestCase):
    def test_two_gifs_keep_mp4_and_order(self):
        result = parse_page(page(tweet_obj()), TARGET)
        self.assertEqual([(m.index, m.extension) for m in result.media], [(1, "mp4"), (2, "mp4")])
        self.assertEqual(result.display_name, "爱丽丝")

    def test_ignore_other_tweets_and_duplicate_ssr(self):
        html = page({"reply": tweet_obj([entity("wrong")], "987654321"), "target": tweet_obj()})
        result = parse_page(html + html, TARGET)
        self.assertEqual([m.media_id for m in result.media], ["11", "22"])

    def test_legacy_photo_original(self):
        obj = tweet_obj([])
        del obj["media_entities2"]
        obj["legacy"] = {"extended_entities": {"media": [{"id_str": "photo1", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/one.jpg"}]}}
        result = parse_page(page(obj), TARGET)
        self.assertIn("name=orig", result.media[0].url)
        self.assertEqual(result.media[0].extension, "jpg")

    def test_highest_direct_variant(self):
        value = entity(kind="video")
        value["video_info"]["variants"].extend([
            {"content_type": "application/x-mpegURL", "bitrate": 9999, "url": "https://video.twimg.com/a.m3u8"},
            {"content_type": "video/mp4", "bitrate": 999, "url": "https://video.twimg.com/high.mp4"}])
        self.assertTrue(media_from_entity(value, 1).url.endswith("high.mp4"))

    def test_actual_gif_is_preserved(self):
        value = entity()
        value["video_info"]["variants"].append({"content_type": "image/gif", "url": "https://pbs.twimg.com/a.gif"})
        self.assertEqual(media_from_entity(value, 1).extension, "gif")

    def test_sensitive_anonymous_failure(self):
        with self.assertRaises(DownloadError) as cm:
            parse_page('<p>This post is only available in the X app</p>', TARGET)
        self.assertEqual(cm.exception.code, "sensitive")

    def test_sensitive_flag_with_access_is_not_blocked(self):
        obj = tweet_obj()
        obj["legacy"] = {"possibly_sensitive": True}
        self.assertEqual(len(parse_page(page(obj), TARGET).media), 2)

    def test_missing_author_not_invented(self):
        obj = tweet_obj()
        obj["core"] = {}
        result = parse_page(page(obj), TARGET)
        self.assertEqual(result.username, TARGET.username)
        self.assertEqual(result.display_name, '名称未知')
        self.assertTrue(result.warning)
        self.assertEqual(len(result.media), 2)

    def test_no_media_distinct_from_parse_failure(self):
        with self.assertRaises(DownloadError) as cm:
            parse_page(page(tweet_obj([])), TARGET)
        self.assertEqual(cm.exception.code, "no_media")

    def test_media_host_validation(self):
        with self.assertRaises(DownloadError):
            validate_media_url("https://video.twimg.com.evil.org/a.mp4")


class StorageTests(unittest.TestCase):
    def test_dedup_and_stable_names_on_retry(self):
        store = Store(folder())
        added, dup = store.add_urls([TARGET, TARGET])
        self.assertEqual((len(added), dup), (1, 1))
        tweet = parse_page(page(tweet_obj()), TARGET)
        store.save_tweet(tweet)
        first = store.media(TARGET.tweet_id)
        store.save_tweet(tweet)
        second = store.media(TARGET.tweet_id)
        self.assertEqual([x["filename"] for x in first], [x["filename"] for x in second])
        self.assertNotEqual(first[0]["unique_id"], first[1]["unique_id"])

    def test_restart_recovers_running_and_retains_done(self):
        store = Store(folder())
        store.add_urls([TARGET])
        store.save_tweet(parse_page(page(tweet_obj()), TARGET))
        store.update_tweet(TARGET.tweet_id, status="downloading")
        store.update_media(TARGET.tweet_id, "11", status="done")
        store.update_media(TARGET.tweet_id, "22", status="downloading")
        recovered = Store(store.base)
        self.assertEqual(recovered.tweet(TARGET.tweet_id)["status"], "queued")
        self.assertEqual([m["status"] for m in recovered.media(TARGET.tweet_id)], ["done", "queued"])


class FileTests(unittest.TestCase):
    def test_image_format_detected_from_content(self):
        path = folder() / "wrong.mp4.part"
        Image.new("RGB", (8, 8), "red").save(path, format="PNG")
        self.assertEqual(verify_file(path)[0], "png")

    def test_bad_file_and_truncated_mp4_rejected(self):
        path = folder() / "bad.mp4"
        for data in (b"<html>Access denied</html>", struct.pack(">I4s4s", 200, b"ftyp", b"isom")):
            path.write_bytes(data)
            with self.assertRaises(DownloadError):
                verify_file(path)

    def test_existing_unowned_file_never_overwritten(self):
        store = Store(folder())
        store.add_urls([TARGET])
        store.save_tweet(parse_page(page(tweet_obj()), TARGET))
        media = store.media(TARGET.tweet_id)[0]
        path = store.download / media["filename"]
        Image.new("RGB", (8, 8)).save(path, "PNG")
        before = path.read_bytes()
        engine = Engine(store)
        engine.run_media(TARGET.tweet_id, media, "direct", "")
        engine.shutdown()
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("校验不符", store.media(TARGET.tweet_id)[0]["error"])

    def test_retry_skips_completed_media(self):
        store = Store(folder())
        store.add_urls([TARGET])
        store.save_tweet(parse_page(page(tweet_obj()), TARGET))
        media = store.media(TARGET.tweet_id)[0]
        path = store.download / media["filename"]
        Image.new("RGB", (8, 8)).save(path, "PNG")
        _, size, digest = verify_file(path)
        store.update_media(TARGET.tweet_id, media["media_id"], sha256=digest, status="done", total=size)
        engine = Engine(store)
        with patch.object(engine, "_download") as download:
            engine.run_media(TARGET.tweet_id, store.media(TARGET.tweet_id)[0], "direct", "")
            download.assert_not_called()
        engine.shutdown()


class AccountTests(unittest.TestCase):
    def test_dpapi_roundtrip_no_plaintext(self):
        path = folder()
        account = AccountStore(path)
        account.save({"auth_token": "dummy-test-secret", "ct0": "dummy-csrf"})
        self.assertNotIn(b"dummy-test-secret", account.path.read_bytes())
        self.assertEqual(AccountStore(path).cookies()["auth_token"], "dummy-test-secret")
        account.clear()
        self.assertFalse(account.path.exists())

    def test_anonymous_session_never_reads_credentials(self):
        with make_session("direct") as session:
            self.assertFalse(session.trust_env)
            self.assertFalse(session.cookies)
            self.assertIsNone(session.auth)

    def test_logged_in_sensitive_media_allowed_with_account_response(self):
        from xdownloader.account import parse_account_payload
        obj = tweet_obj()
        obj["legacy"] = {"possibly_sensitive": True}
        result = parse_account_payload(obj, TARGET)
        self.assertEqual(len(result.media), 2)


class FakeResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data
        self.headers = {"Content-Length": str(len(data)), "Content-Type": "image/png"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeSession:
    def __init__(self, data, fail_second=False):
        self.data, self.fail_second = data, fail_second

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        if self.fail_second and "22.mp4" in url:
            response = FakeResponse(b"denied")
            response.status_code = 403
            return response
        return FakeResponse(self.data)


class EndToEndTests(unittest.TestCase):
    def png(self):
        data = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(data, "PNG")
        return data.getvalue()

    def test_partial_retry_preserves_completed_file(self):
        store = Store(folder())
        store.add_urls([TARGET], gif_mode="mp4")
        engine = Engine(store)
        parsed = parse_page(page(tweet_obj()), TARGET)
        with patch("xdownloader.engine.resolve", return_value=parsed), patch("xdownloader.engine.make_session", return_value=FakeSession(self.png(), True)):
            engine.run_tweet(TARGET.tweet_id)
        self.assertEqual(store.tweet(TARGET.tweet_id)["status"], "failed")
        first = store.media(TARGET.tweet_id)[0]
        self.assertEqual(first["status"], "done")
        self.assertTrue(first["filename"].endswith(".png"))
        timestamp = (store.download / first["filename"]).stat().st_mtime_ns
        with patch("xdownloader.engine.resolve", return_value=parsed), patch("xdownloader.engine.make_session", return_value=FakeSession(self.png())):
            engine.run_tweet(TARGET.tweet_id)
        engine.shutdown()
        self.assertEqual(store.tweet(TARGET.tweet_id)["status"], "done")
        self.assertEqual((store.download / first["filename"]).stat().st_mtime_ns, timestamp)
        self.assertEqual(len(list(store.download.glob("*.png"))), 2)

    def test_network_probe_real_transfer_path_without_queue(self):
        from xdownloader.network import probe_network
        path = folder()
        with patch("xdownloader.network.resolve", return_value=parse_page(page(tweet_obj()), TARGET)), patch("xdownloader.network.make_session", return_value=FakeSession(self.png())):
            result = probe_network(TARGET.url, "direct", "", {}, path)
        self.assertTrue(result["ok"])
        self.assertEqual(list(path.iterdir()), [])

    def test_network_probe_restricted_is_not_success(self):
        from xdownloader.network import probe_network
        with patch("xdownloader.network.resolve", side_effect=DownloadError("sensitive", "敏感内容受限")):
            result = probe_network(TARGET.url, "direct", "", {}, folder())
        self.assertFalse(result["ok"])
        self.assertIn("尚未证明媒体下载链路", result["message"])


if __name__ == "__main__":
    unittest.main()
