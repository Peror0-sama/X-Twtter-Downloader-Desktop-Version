"""X 广播回放解析，复用账号命名与下载历史。"""
from __future__ import annotations

from urllib.parse import urlsplit
from .core import DownloadError, Media, Tweet, _walk, author_identity, validate_media_url


def parse_broadcast(soup, documents, target):
    for document in documents:
        for obj in _walk(document):
            if str(obj.get('broadcast_id', '')) != target.tweet_id:
                continue
            if obj.get('state') in ('Running', 'NotStarted'):
                raise DownloadError('broadcast_live', '该广播尚未结束，请在回放生成后重试')
            if obj.get('available_for_replay') is False:
                raise DownloadError('broadcast_unavailable', '此广播未提供可下载的回放')
            user, display = author_identity({'core': {'user_results': obj.get('user_results', {})}}, documents)
            warnings = []
            if not user:
                user = '未知账号'
                warnings.append('未能取得广播作者账号，文件名暂用“未知账号”')
            if not display:
                display = '名称未知'
                warnings.append('未能取得作者显示名称，文件名暂用“名称未知”')
            tag = soup.find('meta', attrs={'property': 'og:video:url'}) or soup.find('meta', attrs={'property': 'og:video:secure_url'})
            url = obj.get('hlsUrl') or (tag.get('content') if tag else '')
            if not url:
                raise DownloadError('parse', 'X 尚未返回广播回放地址')
            validate_media_url(url)
            if not urlsplit(url).path.endswith('.m3u8'):
                raise DownloadError('broadcast_format', '广播未返回受支持的 HLS 回放地址')
            return Tweet(target.tweet_id, user, display, [Media(target.tweet_id, 1, 'video', url, 'mp4')], '；'.join(warnings))
    raise DownloadError('parse', '未能取得目标广播信息，请检查链接、登录或访问权限')
