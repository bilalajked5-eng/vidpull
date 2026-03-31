from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import subprocess
import os
import re
import json
import tempfile
import threading
import uuid

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Helpers ───
def sanitize(s):
    return re.sub(r'[^\w\s\-.]', '', str(s))[:80]

def get_format_size(fmt):
    fs = fmt.get('filesize') or fmt.get('filesize_approx')
    if not fs:
        return 'Unknown'
    for unit in ['B','KB','MB','GB']:
        if fs < 1024:
            return f"{fs:.1f} {unit}"
        fs /= 1024
    return f"{fs:.1f} GB"

# ─── API: Get video info ───
@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.get_json()
    url  = (data or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
        'extract_flat': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if 'Unsupported URL' in msg:
            return jsonify({'error': 'This platform is not supported or the URL is invalid.'}), 400
        if 'Private' in msg or 'private' in msg:
            return jsonify({'error': 'This video is private and cannot be downloaded.'}), 403
        return jsonify({'error': f'Could not fetch video: {msg[:200]}'}), 400
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500

    title     = info.get('title', 'Untitled Video')
    duration  = info.get('duration')
    uploader  = info.get('uploader') or info.get('channel') or ''
    thumbnail = info.get('thumbnail', '')
    extractor = info.get('extractor_key', info.get('extractor', 'Unknown'))
    webpage   = info.get('webpage_url', url)

    # Duration string
    dur_str = ''
    if duration:
        m, s = divmod(int(duration), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # Build quality options
    formats = info.get('formats') or []
    qualities = []
    seen_heights = set()

    # Video+audio merged formats
    for fmt in reversed(formats):
        h = fmt.get('height')
        vcodec = fmt.get('vcodec', '')
        acodec = fmt.get('acodec', '')
        if not h or vcodec in ('none', None) or acodec in ('none', None):
            continue
        if h in seen_heights:
            continue
        seen_heights.add(h)
        label = f"{h}p"
        if h >= 2160: label = "4K (2160p)"
        elif h >= 1440: label = "2K (1440p)"
        elif h >= 1080: label = "Full HD (1080p)"
        elif h >= 720:  label = "HD (720p)"
        elif h >= 480:  label = "480p"
        elif h >= 360:  label = "360p"
        qualities.append({
            'format_id': fmt['format_id'],
            'label': label,
            'height': h,
            'ext': fmt.get('ext', 'mp4'),
            'size': get_format_size(fmt),
            'type': 'video',
        })

    # If no merged formats, add best video+audio combo
    if not qualities:
        best_v = None
        best_a = None
        for fmt in reversed(formats):
            h = fmt.get('height')
            vcodec = fmt.get('vcodec', '')
            acodec = fmt.get('acodec', '')
            if h and vcodec not in ('none', None) and acodec in ('none', None) and best_v is None:
                best_v = fmt
            if acodec not in ('none', None) and vcodec in ('none', None) and best_a is None:
                best_a = fmt
        if best_v and best_a:
            h = best_v.get('height', 0)
            label = f"{h}p" if h else "Best Quality"
            qualities.append({
                'format_id': f"{best_v['format_id']}+{best_a['format_id']}",
                'label': label,
                'height': h,
                'ext': 'mp4',
                'size': 'Varies',
                'type': 'video',
            })
        elif best_v:
            h = best_v.get('height', 0)
            qualities.append({
                'format_id': best_v['format_id'],
                'label': f"{h}p" if h else "Best",
                'height': h,
                'ext': best_v.get('ext', 'mp4'),
                'size': get_format_size(best_v),
                'type': 'video',
            })

    # Sort by height descending
    qualities.sort(key=lambda x: x.get('height', 0), reverse=True)

    # Limit to top 4 video qualities
    qualities = qualities[:4]

    # Add MP3 audio option
    qualities.append({
        'format_id': 'bestaudio',
        'label': 'MP3 (Audio Only)',
        'height': 0,
        'ext': 'mp3',
        'size': 'Varies',
        'type': 'audio',
    })

    return jsonify({
        'title': title,
        'uploader': uploader,
        'duration': dur_str,
        'thumbnail': thumbnail,
        'platform': extractor,
        'webpage_url': webpage,
        'qualities': qualities,
        'url': url,
    })


# ─── API: Download video (streamed) ───
@app.route('/api/download', methods=['POST'])
def download_video():
    data      = request.get_json()
    url       = (data or {}).get('url', '').strip()
    format_id = (data or {}).get('format_id', 'best')
    is_audio  = (data or {}).get('type', 'video') == 'audio'
    ext       = (data or {}).get('ext', 'mp4')
    title     = sanitize((data or {}).get('title', 'video'))

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    tmp_dir = tempfile.mkdtemp()
    out_template = os.path.join(tmp_dir, '%(title)s.%(ext)s')

    if is_audio:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_template,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        download_ext = 'mp3'
        mime = 'audio/mpeg'
    else:
        ydl_opts = {
            'format': f'{format_id}+bestaudio[ext=m4a]/best[height<={_height_from_fmt(format_id)}]/{format_id}/best',
            'outtmpl': out_template,
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'merge_output_format': 'mp4',
        }
        download_ext = 'mp4'
        mime = 'video/mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return jsonify({'error': str(e)[:300]}), 500

    # Find the downloaded file
    files = os.listdir(tmp_dir)
    if not files:
        return jsonify({'error': 'Download failed — no file produced.'}), 500

    filepath = os.path.join(tmp_dir, files[0])
    actual_ext = os.path.splitext(files[0])[1].lstrip('.')
    filename = f"{title}.{actual_ext}"

    def generate():
        try:
            with open(filepath, 'rb') as f:
                while chunk := f.read(1024 * 256):
                    yield chunk
        finally:
            try: os.remove(filepath)
            except: pass
            try: os.rmdir(tmp_dir)
            except: pass

    fsize = os.path.getsize(filepath)
    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Content-Length': str(fsize),
        'Content-Type': mime,
        'X-Filename': filename,
    }

    return Response(
        stream_with_context(generate()),
        headers=headers,
        mimetype=mime
    )

def _height_from_fmt(fmt_id):
    """Try to guess a height cap from format string like '137'."""
    return 9999

if __name__ == '__main__':
    print("\n✅  VidPull backend running at http://localhost:5000\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
