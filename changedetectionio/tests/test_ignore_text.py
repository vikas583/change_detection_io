#!/usr/bin/python3

import time
from flask import url_for
from . util import live_server_setup
from changedetectionio import html_tools

def test_setup(live_server):
    live_server_setup(live_server)

# Unit test of the stripper
# Always we are dealing in utf-8
def test_strip_text_func():
    from changedetectionio import fetch_site_status

    test_content = """
    Some content
    is listed here

    but sometimes we want to remove the lines.

    but not always."""

    ignore_lines = ["sometimes"]

    fetcher = fetch_site_status.perform_site_check(datastore=False)
    stripped_content = html_tools.strip_ignore_text(test_content, ignore_lines)

    assert b"sometimes" not in stripped_content
    assert b"Some content" in stripped_content


def set_original_ignore_response():
    test_return_data = """<html>
       <body>
     Some initial text</br>
     <p>Which is across multiple lines</p>
     </br>
     So let's see what happens.  </br>
     </body>
     </html>

    """

    with open("test-datastore/endpoint-content.txt", "w") as f:
        f.write(test_return_data)


def set_modified_original_ignore_response():
    test_return_data = """<html>
       <body>
     Some NEW nice initial text</br>
     <p>Which is across multiple lines</p>
     </br>
     So let's see what happens.  </br>
     <p>new ignore stuff</p>
     <p>blah</p>
     </body>
     </html>

    """

    with open("test-datastore/endpoint-content.txt", "w") as f:
        f.write(test_return_data)


# Is the same but includes ZZZZZ, 'ZZZZZ' is the last line in ignore_text
def set_modified_ignore_response():
    test_return_data = """<html>
       <body>
     Some initial text</br>
     <p>Which is across multiple lines</p>
     <P>ZZZZz</P>
     </br>
     So let's see what happens.  </br>
     </body>
     </html>

    """

    with open("test-datastore/endpoint-content.txt", "w") as f:
        f.write(test_return_data)


def test_check_ignore_text_functionality(client, live_server):
    sleep_time_for_fetch_thread = 3

    # Use a mix of case in ZzZ to prove it works case-insensitive.
    ignore_text = "XXXXX\r\nYYYYY\r\nzZzZZ\r\nnew ignore stuff"
    set_original_ignore_response()

    # Give the endpoint time to spin up
    time.sleep(1)

    # Add our URL to the import page
    test_url = url_for('test_endpoint', _external=True)
    res = client.post(
        url_for("import_page"),
        data={"urls": test_url},
        follow_redirects=True
    )
    assert b"1 Imported" in res.data

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)

    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)

    # Goto the edit page, add our ignore text
    # Add our URL to the import page
    res = client.post(
        url_for("edit_page", uuid="first"),
        data={"ignore_text": ignore_text, "url": test_url, 'fetch_backend': "html_requests"},
        follow_redirects=True
    )
    assert b"Updated watch." in res.data

    # Check it saved
    res = client.get(
        url_for("edit_page", uuid="first"),
    )
    assert bytes(ignore_text.encode('utf-8')) in res.data

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)

    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)

    # It should report nothing found (no new 'unviewed' class)
    res = client.get(url_for("index"))
    assert b'unviewed' not in res.data
    assert b'/test-endpoint' in res.data

    #  Make a change
    set_modified_ignore_response()

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)
    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)

    # It should report nothing found (no new 'unviewed' class)
    res = client.get(url_for("index"))
    assert b'unviewed' not in res.data
    assert b'/test-endpoint' in res.data





    # Just to be sure.. set a regular modified change..
    set_modified_original_ignore_response()
    client.get(url_for("api_watch_checknow"), follow_redirects=True)
    time.sleep(sleep_time_for_fetch_thread)

    res = client.get(url_for("index"))
    assert b'unviewed' in res.data

    # Check the preview/highlighter, we should be able to see what we ignored, but it should be highlighted
    # We only introduce the "modified" content that includes what we ignore so we can prove the newest version also displays
    # at /preview
    res = client.get(url_for("preview_page", uuid="first"))
    # We should be able to see what we ignored
    assert b'<div class="ignored">new ignore stuff' in res.data

    res = client.get(url_for("api_delete", uuid="all"), follow_redirects=True)
    assert b'Deleted' in res.data

def test_check_global_ignore_text_functionality(client, live_server):
    sleep_time_for_fetch_thread = 3

    # Give the endpoint time to spin up
    time.sleep(1)

    ignore_text = "XXXXX\r\nYYYYY\r\nZZZZZ"
    set_original_ignore_response()

    # Goto the settings page, add our ignore text
    res = client.post(
        url_for("settings_page"),
        data={
            "requests-time_between_check-minutes": 180,
            "application-global_ignore_text": ignore_text,
            'application-fetch_backend': "html_requests"
        },
        follow_redirects=True
    )
    assert b"Settings updated." in res.data


    # Add our URL to the import page
    test_url = url_for('test_endpoint', _external=True)
    res = client.post(
        url_for("import_page"),
        data={"urls": test_url},
        follow_redirects=True
    )
    assert b"1 Imported" in res.data

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)

    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)


    # Goto the edit page of the item, add our ignore text
    # Add our URL to the import page
    res = client.post(
        url_for("edit_page", uuid="first"),
        data={"ignore_text": "something irrelevent but just to check", "url": test_url, 'fetch_backend': "html_requests"},
        follow_redirects=True
    )
    assert b"Updated watch." in res.data

    # Check it saved
    res = client.get(
        url_for("settings_page"),
    )
    assert bytes(ignore_text.encode('utf-8')) in res.data

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)

    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)

    # so that we are sure everything is viewed and in a known 'nothing changed' state
    res = client.get(url_for("diff_history_page", uuid="first"))

    # It should report nothing found (no new 'unviewed' class)
    res = client.get(url_for("index"))
    assert b'unviewed' not in res.data
    assert b'/test-endpoint' in res.data


    #  Make a change which includes the ignore text
    set_modified_ignore_response()

    # Trigger a check
    client.get(url_for("api_watch_checknow"), follow_redirects=True)
    # Give the thread time to pick it up
    time.sleep(sleep_time_for_fetch_thread)

    # It should report nothing found (no new 'unviewed' class)
    res = client.get(url_for("index"))
    assert b'unviewed' not in res.data
    assert b'/test-endpoint' in res.data

    # Just to be sure.. set a regular modified change that will trigger it
    set_modified_original_ignore_response()
    client.get(url_for("api_watch_checknow"), follow_redirects=True)
    time.sleep(sleep_time_for_fetch_thread)
    res = client.get(url_for("index"))
    assert b'unviewed' in res.data

    res = client.get(url_for("api_delete", uuid="all"), follow_redirects=True)
    assert b'Deleted' in res.data
