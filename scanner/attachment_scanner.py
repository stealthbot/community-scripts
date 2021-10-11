
from datetime import datetime
import json
import os
import time

import requests


URL_BASE = "http://www.stealthbot.net/forum/index.php"
RESULTS_DIR = "files"
SUMMARY_FILE = "results.json"

RESCAN_FILES = True


def determine_filename(meta):
    name = meta.get("name")
    version = meta.get("version")
    file_name = "%06i.%s" % (meta["attach_id"], meta['extension'])
    if name:
        file_name = ("%s_v%s_%s" % (name, version, file_name)) if version else ("%s_%s" % (name, file_name))
    return file_name


def parse_text_for_script_meta(text, meta):
    # Probably a script? or some kind of text
    meta["extension"] = "txt"
    check_lines = [line.strip() for line in text.splitlines(keepends=False)]

    header = check_lines[0]
    if len(header) > 1 and header[0] == "'" and header[1] != '/' and ' ' not in header:
        # This is most likely an old-style plugin
        meta["extension"] = "plug"
        meta["name"] = header[1:]

        header = check_lines[1]
        if len(header) > 1 and header[0] == "'" and header[1].isdigit() and '.' in header and ' ' not in header:
            meta["version"] = header[1:]

    # Just in case it's a false-flag...
    def is_header(head):
        return head.lower().startswith('script(') and '=' in head

    def parse_header_line(head):
        return head.split('"')[1].split('"')[0].lower(), head.split('=', maxsplit=1)[1].strip().strip('"')

    v_keys = ["major", "minor", "revision"]
    version = ["0", "0", "0"]
    for line in check_lines:
        if is_header(line):
            meta["extension"] = "txt"
            key, value = parse_header_line(line)
            if key in ("name", "author", "description"):
                meta[key] = value
            elif key in v_keys:
                version[v_keys.index(key)] = value
        else:
            words = line.lower().split()
            if len(words) in [2, 3] and line[0] != "'" and words[-1][-1] == ')':
                if "sub" in words or "function" in words:
                    # Starting actual code now, probably done with the header
                    break

    version = '.'.join(version)
    if "name" in meta or version != "0.0.0":
        if version != "0.0.0" or "version" not in meta:
            meta["version"] = version
    return meta


def fetch_attachment(attach_id):
    params = {
        "app": "core", "module": "attach", "section": "attach", "attach_id": attach_id
    }
    r = requests.get(URL_BASE, params=params)
    content_type = r.headers['Content-Type']
    meta = {"attach_id": attach_id, "type": content_type}

    if r.status_code != 200:
        print("Attachment #%i returned error %i - %s" % (attach_id, r.status_code, r.reason))
        meta["error"] = r.reason
        return False, meta, r

    if content_type.startswith("text/html"):
        # Probably an error page
        html = r.text

        if "<h2>An Error Occurred</h2>" in html:
            # Definitely an error page
            del meta["type"]
            try:
                error = html.split("<div class='message error'>")[1].split("</div>")[0].strip()
                meta["error"] = error
                print("Attachment #%i returned error: %s" % (attach_id, error))
                return False, meta, r
            except IndexError:
                print("An error occurred extracting the error message from attachment #%i" % attach_id)
                meta["error"] = "IPB parse fail"
                return False, meta, r

    elif content_type == "unknown/unknown":
        meta = parse_text_for_script_meta(r.text, meta)
        return True, meta, r

    elif content_type == "application/zip":
        print("Attachment #%i found - is an archive/zip" % attach_id)
        meta["extension"] = "zip"
        return True, meta, r

    else:
        print("Attachment #%i found, but probably not a script - type: %s" % (attach_id, r.headers['Content-Type']))
        return False, meta, r


def read_file_value(fh):
    return fh.readline().split(':')[1].strip()


def read_summary(file_path):
    try:
        with open(file_path, 'r') as fh:
            summary = json.load(fh)
        return summary["index"], summary["elapsed"], summary["results"]
    except (KeyError, json.JSONDecodeError) as ex:
        print("Summary file is corrupt: %s" % ex)
        return 1, 0, {}


def reparse_scripts():
    if not os.path.isfile(SUMMARY_FILE):
        print("No scripts found to reparse")
        return {}

    # Read the summary, we are only interested in the results value
    with open(SUMMARY_FILE, 'r') as fh:
        summary = json.load(fh)
        results = summary["results"]

    # Re-read each file that was saved and update its metadata
    try:
        for attach_id, meta in results.items():
            if "path" in meta:
                ext = os.path.splitext(meta['path'])[1]
                if ext not in [".txt", ".plug"]:
                    print("File '%s' is not a script, skipping" % meta['path'])
                    continue

                file_path = meta['path']
                base_dir, current_name = os.path.split(file_path)
                if not os.path.isfile(file_path):
                    # File may have been errantly renamed... check for its attachment ID
                    print("File '%s' not found, checking for attachment ID..." % file_path)
                    id_suffix = "%06i" % int(attach_id)
                    found = False

                    for possible_file in os.listdir(base_dir):
                        if os.path.splitext(possible_file)[0].endswith(id_suffix):
                            meta["path"] = file_path = os.path.join(base_dir, possible_file)
                            current_name = possible_file
                            print("Located file #%i as '%s'" % (int(attach_id), current_name))
                            found = True
                            break

                    if not found:
                        print("File '%s' could not be located. Skipping" % current_name)
                        continue

                with open(file_path, 'r') as fh:
                    print("Re-parsing file: %s" % file_path)
                    parse_text_for_script_meta(fh.read(), meta)

                new_name = determine_filename(meta)
                if current_name != new_name:
                    new_path = os.path.join(base_dir, new_name)
                    os.rename(file_path, new_path)
                    print("Renamed file '%s' to '%s'" % (current_name, new_name))
                    meta['path'] = new_path     # Only update this after the rename succeeded
    finally:
        with open(SUMMARY_FILE, 'w') as fh:
            json.dump(summary, fh, indent=4)
    return results


def main():
    results = {}
    index = 1
    runtime = 0

    # Check for previous results (resume)
    if os.path.isfile(SUMMARY_FILE):
        index, runtime, results = read_summary(SUMMARY_FILE)
        if len(results) > 0:
            if RESCAN_FILES:
                print("Previous results found. Re-parsing local copies...")
                results = reparse_scripts()
                print("Resuming scan from index %s" % index)
            else:
                print("Previous results found. Resuming from index %i" % index)

    start = datetime.now()
    try:
        while True:
            success, meta, data = fetch_attachment(index)
            results[index] = meta

            if success:
                if not os.path.isdir(RESULTS_DIR):
                    os.mkdir(RESULTS_DIR)

                try:
                    file_name = os.path.join(RESULTS_DIR, determine_filename(meta))
                    if meta["extension"] not in ["txt", "plug"]:
                        with open(file_name, 'wb') as fh:
                            fh.write(data.content)
                    else:
                        encoding = data.apparent_encoding or 'cp1252'
                        with open(file_name, 'w', encoding=encoding) as fh:
                            fh.write(data.text.replace('\r\n', '\n'))

                except UnicodeEncodeError as ex:
                    print("Attachment #%i, unicode error: %s" % (index, ex))
                else:
                    results[index]["path"] = file_name
                    print("Attachment #%i saved as %s" % (index, file_name))

            index += 1
            time.sleep(1)

    finally:
        end = datetime.now()
        elapsed = (end - start).total_seconds()
        runtime = runtime + elapsed

        summary = {
            "index": index, "elapsed": runtime, "time": end.isoformat(), "results": results
        }
        with open(SUMMARY_FILE, 'w') as fh:
            json.dump(summary, fh, indent=4)

        print("Time elapsed (run/total): %i/%i seconds" % (elapsed, runtime))


if __name__ == "__main__":
    main()
