import asyncio
import hashlib
import random
import os
import re
import configparser
import traceback

import requests

# 读取 config.ini 文件
config = configparser.ConfigParser()
config.read('config.ini')

MAX_CONCURRENT_TASKS = int(config['settings']['max_concurrent_tasks'])
DURATION = int(config['settings']['duration'])
COLLECT_COUNT = int(config['settings']['collect_count'])
MIN_RESOLUTION_W = int(config['settings']['min_resolution'].split("x")[0])
MIN_RESOLUTION_H = int(config['settings']['min_resolution'].split("x")[1])

semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
successful_streams = []
streams_lock = asyncio.Lock()


def get_channel_items():
    try:
        template_file = "template.txt"
        with open(template_file, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        channels = {}
        channel_keys = set()
        current_category = ""
        pattern = r"^(.*?),(?!#genre#)(.*?)$"
        for line in lines:
            line = line.strip()
            if "#genre#" in line:
                current_category = line.split(",")[0]
                channels[current_category] = {}
            else:
                match = re.search(pattern, line)
                if match:
                    channel_keys.add(filter_cctv_key(match.group(1)))
                    if match.group(1) not in channels[current_category]:
                        channels[current_category][match.group(1)] = [match.group(2)]
                    else:
                        channels[current_category][match.group(1)].append(match.group(2))
                else:
                    channel_keys.add(filter_cctv_key(line))
                    channels[current_category][line] = []
        return channels, channel_keys
    finally:
        f.close()


def filter_cctv_key(key: str):
    key = re.sub(r'\[.*?\]', '', key)
    if "cctv" not in key.lower():
        return key
    key_ports = key.split()
    if key_ports[0].lower() == "cctv":
        key = key_ports[0] + key_ports[1]
    else:
        key = key_ports[0]
    chinese_pattern = re.compile("[\u4e00-\u9fa5]+")
    filtered_text = chinese_pattern.sub('', key)
    result = re.sub(r'\[\d+\*\d+\]', '', filtered_text)
    if "-" not in result:
        result = result.replace("CCTV", "CCTV-")
    if result.upper().endswith("HD"):
        result = result[:-2]
    return result.strip()


def get_test_speed_channels():
    try:
        channels, channel_keys = get_channel_items()
        print(channels)
        response = requests.get(
            url="https://up.myzy.us.kg/https://raw.githubusercontent.com/kimwang1978/collect-tv-txt/main/merged_output.txt",
            timeout=30)
        lines = response.text.splitlines()
        test_speed_channels = {}
        for line in lines:
            if "#genre#" in line:
                continue
            if "," not in line:
                continue
            channel_name = filter_cctv_key(line.split(",")[0])
            if channel_name not in channel_keys:
                continue
            if test_speed_channels.get(channel_name, None):
                test_speed_channels[channel_name].append(line.split(",")[1])
            else:
                test_speed_channels[channel_name] = [line.split(",")[1]]

        return test_speed_channels
    except requests.RequestException as e:
        print(f"Error fetching channel list: {e}")
        return {}


async def ffmpeg_url(video_url, timeout, output_file, cmd='ffmpeg'):
    args = [cmd, '-t', str(timeout), '-stats', '-i', video_url, "-c", "copy", output_file]
    proc = None
    res = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
        if out:
            res = out.decode('utf-8')
        if err:
            res = err.decode('utf-8')
    except asyncio.TimeoutError:
        if proc:
            proc.kill()
    except Exception as e:
        if proc:
            proc.kill()
    finally:
        if proc:
            await proc.wait()
        return res


async def measure_video_stream_speed(video_url):
    global successful_streams

    random_number = str(random.randint(1, 1000000))
    # 生成视频 URL 和随机数的组合哈希值
    hash_code = hashlib.md5((video_url + random_number).encode()).hexdigest()
    output_file = f"output_{hash_code}.ts"

    async with semaphore:
        try:
            async with streams_lock:
                if len(successful_streams) >= COLLECT_COUNT:
                    return

            ffmpeg_output = await ffmpeg_url(video_url, DURATION, output_file)
            if not ffmpeg_output:
                return

            resolution_match = re.search(r'Video:.* (\d+)x(\d+)', ffmpeg_output)
            if resolution_match:
                width, height = int(resolution_match.group(1)), int(resolution_match.group(2))
                if width < MIN_RESOLUTION_W or height < MIN_RESOLUTION_W:
                    return
                if os.path.exists(output_file):
                    file_size = os.path.getsize(output_file)
                    if file_size <= 0:
                        return

                    t = DURATION - 5 if DURATION > 5 else DURATION
                    speed = file_size / 1024 / 1024 / t

                    # if speed < (bitrate / 8) * DURATION:
                    #     return None
                    # else:
                    async with streams_lock:
                        if len(successful_streams) < COLLECT_COUNT:
                            successful_streams.append((video_url, speed))
                            return
                        if len(successful_streams) >= COLLECT_COUNT:
                            return
        except Exception:
            traceback.print_exc()
            return
        finally:
            if os.path.exists(output_file):
                os.remove(output_file)


async def main(video_urls):
    tasks = [measure_video_stream_speed(url) for url in video_urls]
    await asyncio.gather(*tasks)
    sorted_streams = sorted(successful_streams, key=lambda x: x[1], reverse=True)
    return [url for url, speed in sorted_streams]


if __name__ == '__main__':
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError as e:
        if "There is no current event loop" in str(e):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

    # 执行任务
    test_speed_channels = get_test_speed_channels()

    test_success_channels = {}
    # 测试视频流 URLs
    for channel, video_urls in test_speed_channels.items():
        print(channel, len(video_urls))
        sorted_urls = loop.run_until_complete(main(video_urls))  # 使用当前事件循环
        test_success_channels[channel] = sorted_urls
        print("Sorted URLs by speed:", sorted_urls)

    channels, _ = get_channel_items()
    output = ""
    for category, channels in channels.items():
        output += f"{category},#genre#\n"
        for channel, _ in channels.items():
            urls = test_success_channels.get(channel, [])
            for url in urls:
                output += f"{channel},{url}\n"
    with open("result.txt", "w", encoding="utf-8") as file:
        file.write(output)
    all_files = [f for f in os.listdir(os.getcwd()) if os.path.isfile(os.path.join(os.getcwd(), f))]
    for file in all_files:
        if file.startswith("output_"):
            os.remove(file)
