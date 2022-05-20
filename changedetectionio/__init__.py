#!/usr/bin/python3


# @todo logging
# @todo extra options for url like , verify=False etc.
# @todo enable https://urllib3.readthedocs.io/en/latest/user-guide.html#ssl as option?
# @todo option for interval day/6 hour/etc
# @todo on change detected, config for calling some API
# @todo fetch title into json
# https://distill.io/features
# proxy per check
#  - flask_cors, itsdangerous,MarkupSafe

import datetime
import os
import queue
import threading
import time
from copy import deepcopy
from threading import Event

import flask_login
import pytz
import timeago
from feedgen.feed import FeedGenerator
from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_login import login_required
from flask_wtf import CSRFProtect
from flask_sqlalchemy import SQLAlchemy
from changedetectionio.model.WebScreenshots import WebScreenshots
from changedetectionio import html_tools


__version__ = '0.39.12'

datastore = None

# Local
running_update_threads = []
ticker_thread = None

extra_stylesheets = []

update_q = queue.Queue()

notification_q = queue.Queue()

app = Flask(__name__,
            static_url_path="",
            static_folder="static",
            template_folder="templates")

basedir = os.path.expandvars(r'%APPDATA%\changedetection.io')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'web_intelligence.db')
# print('sqlite:///' + os.path.join(basedir, 'web_intelligence.db'))

db = SQLAlchemy(app)
# Stop browser caching of assets
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

app.config.exit = Event()

app.config['NEW_VERSION_AVAILABLE'] = False

app.config['LOGIN_DISABLED'] = False

#app.config["EXPLAIN_TEMPLATE_LOADING"] = True

# Disables caching of the templates
app.config['TEMPLATES_AUTO_RELOAD'] = True

csrf = CSRFProtect()
csrf.init_app(app)

notification_debug_log=[]

def init_app_secret(datastore_path):
    secret = ""

    path = "{}/secret.txt".format(datastore_path)

    try:
        with open(path, "r") as f:
            secret = f.read()

    except FileNotFoundError:
        import secrets
        with open(path, "w") as f:
            secret = secrets.token_hex(32)
            f.write(secret)

    return secret

# We use the whole watch object from the store/JSON so we can see if there's some related status in terms of a thread
# running or something similar.
@app.template_filter('format_last_checked_time')
def _jinja2_filter_datetime(watch_obj, format="%Y-%m-%d %H:%M:%S"):
    # Worker thread tells us which UUID it is currently processing.
    for t in running_update_threads:
        if t.current_uuid == watch_obj['uuid']:
            return "Checking now.."

    if watch_obj['last_checked'] == 0:
        return 'Not yet'

    return timeago.format(int(watch_obj['last_checked']), time.time())


# @app.context_processor
# def timeago():
#    def _timeago(lower_time, now):
#        return timeago.format(lower_time, now)
#    return dict(timeago=_timeago)

@app.template_filter('format_timestamp_timeago')
def _jinja2_filter_datetimestamp(timestamp, format="%Y-%m-%d %H:%M:%S"):
    return timeago.format(timestamp, time.time())
    # return timeago.format(timestamp, time.time())
    # return datetime.datetime.utcfromtimestamp(timestamp).strftime(format)

# When nobody is logged in Flask-Login's current_user is set to an AnonymousUser object.
class User(flask_login.UserMixin):
    id=None

    def set_password(self, password):
        return True
    def get_user(self, email="defaultuser@changedetection.io"):
        return self
    def is_authenticated(self):
        return True
    def is_active(self):
        return True
    def is_anonymous(self):
        return False
    def get_id(self):
        return str(self.id)

    # Compare given password against JSON store or Env var
    def check_password(self, password):

        import base64
        import hashlib

        # Can be stored in env (for deployments) or in the general configs
        raw_salt_pass = os.getenv("SALTED_PASS", False)

        if not raw_salt_pass:
            raw_salt_pass = datastore.data['settings']['application']['password']

        raw_salt_pass = base64.b64decode(raw_salt_pass)


        salt_from_storage = raw_salt_pass[:32]  # 32 is the length of the salt

        # Use the exact same setup you used to generate the key, but this time put in the password to check
        new_key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),  # Convert the password to bytes
            salt_from_storage,
            100000
        )
        new_key =  salt_from_storage + new_key

        return new_key == raw_salt_pass

    pass

def changedetection_app(config=None, datastore_o=None):
    global datastore
    datastore = datastore_o

    #app.config.update(config or {})

    login_manager = flask_login.LoginManager(app)
    login_manager.login_view = 'login'
    app.secret_key = init_app_secret(config['datastore_path'])

    # Setup cors headers to allow all domains
    # https://flask-cors.readthedocs.io/en/latest/
    #    CORS(app)

    @login_manager.user_loader
    def user_loader(email):
        user = User()
        user.get_user(email)
        return user

    @login_manager.unauthorized_handler
    def unauthorized_handler():
        # @todo validate its a URL of this host and use that
        return redirect(url_for('login', next=url_for('index')))

    @app.route('/logout')
    def logout():
        flask_login.logout_user()
        return redirect(url_for('index'))

    # https://github.com/pallets/flask/blob/93dd1709d05a1cf0e886df6223377bdab3b077fb/examples/tutorial/flaskr/__init__.py#L39
    # You can divide up the stuff like this
    @app.route('/login', methods=['GET', 'POST'])
    def login():

        if not datastore.data['settings']['application']['password'] and not os.getenv("SALTED_PASS", False):
            flash("Login not required, no password enabled.", "notice")
            return redirect(url_for('index'))

        if request.method == 'GET':
            if flask_login.current_user.is_authenticated:
                flash("Already logged in")
                return redirect(url_for("index"))

            output = render_template("login.html")
            return output

        user = User()
        user.id = "defaultuser@changedetection.io"

        password = request.form.get('password')

        if (user.check_password(password)):
            flask_login.login_user(user, remember=True)

            # For now there's nothing else interesting here other than the index/list page
            # It's more reliable and safe to ignore the 'next' redirect
            # When we used...
            # next = request.args.get('next')
            # return redirect(next or url_for('index'))
            # We would sometimes get login loop errors on sites hosted in sub-paths

            # note for the future:
            #            if not is_safe_url(next):
            #                return flask.abort(400)
            return redirect(url_for('index'))

        else:
            flash('Incorrect password', 'error')

        return redirect(url_for('login'))

    @app.before_request
    def do_something_whenever_a_request_comes_in():

        # Disable password login if there is not one set
        # (No password in settings or env var)
        app.config['LOGIN_DISABLED'] = datastore.data['settings']['application']['password'] == False and os.getenv("SALTED_PASS", False) == False

        # Set the auth cookie path if we're running as X-settings/X-Forwarded-Prefix
        if os.getenv('USE_X_SETTINGS') and 'X-Forwarded-Prefix' in request.headers:
            app.config['REMEMBER_COOKIE_PATH'] = request.headers['X-Forwarded-Prefix']
            app.config['SESSION_COOKIE_PATH'] = request.headers['X-Forwarded-Prefix']

        # For the RSS path, allow access via a token
        if request.path == '/rss' and request.args.get('token'):
            app_rss_token = datastore.data['settings']['application']['rss_access_token']
            rss_url_token = request.args.get('token')
            if app_rss_token == rss_url_token:
                app.config['LOGIN_DISABLED'] = True

    @app.route("/rss", methods=['GET'])
    @login_required
    def rss():
        from . import diff
        limit_tag = request.args.get('tag')

        # Sort by last_changed and add the uuid which is usually the key..
        sorted_watches = []

        # @todo needs a .itemsWithTag() or something
        for uuid, watch in datastore.data['watching'].items():

            if limit_tag != None:
                # Support for comma separated list of tags.
                for tag_in_watch in watch['tag'].split(','):
                    tag_in_watch = tag_in_watch.strip()
                    if tag_in_watch == limit_tag:
                        watch['uuid'] = uuid
                        sorted_watches.append(watch)

            else:
                watch['uuid'] = uuid
                sorted_watches.append(watch)

        sorted_watches.sort(key=lambda x: x['last_changed'], reverse=True)

        fg = FeedGenerator()
        fg.title('changedetection.io')
        fg.description('Feed description')
        fg.link(href='https://changedetection.io')

        for watch in sorted_watches:

            dates = list(watch['history'].keys())
            # Re #521 - Don't bother processing this one if theres less than 2 snapshots, means we never had a change detected.
            if len(dates) < 2:
                continue

            # Convert to int, sort and back to str again
            # @todo replace datastore getter that does this automatically
            dates = [int(i) for i in dates]
            dates.sort(reverse=True)
            dates = [str(i) for i in dates]
            prev_fname = watch['history'][dates[1]]

            if not watch['viewed']:
                # Re #239 - GUID needs to be individual for each event
                # @todo In the future make this a configurable link back (see work on BASE_URL https://github.com/dgtlmoon/changedetection.io/pull/228)
                guid = "{}/{}".format(watch['uuid'], watch['last_changed'])
                fe = fg.add_entry()


                # Include a link to the diff page, they will have to login here to see if password protection is enabled.
                # Description is the page you watch, link takes you to the diff JS UI page
                base_url = datastore.data['settings']['application']['base_url']
                if base_url == '':
                    base_url = "<base-url-env-var-not-set>"

                diff_link = {'href': "{}{}".format(base_url, url_for('diff_history_page', uuid=watch['uuid']))}

                fe.link(link=diff_link)

                # @todo watch should be a getter - watch.get('title') (internally if URL else..)

                watch_title = watch.get('title') if watch.get('title') else watch.get('url')
                fe.title(title=watch_title)
                latest_fname = watch['history'][dates[0]]

                html_diff = diff.render_diff(prev_fname, latest_fname, include_equal=False, line_feed_sep="</br>")
                fe.description(description="<![CDATA[<html><body><h4>{}</h4>{}</body></html>".format(watch_title, html_diff))

                fe.guid(guid, permalink=False)
                dt = datetime.datetime.fromtimestamp(int(watch['newest_history_key']))
                dt = dt.replace(tzinfo=pytz.UTC)
                fe.pubDate(dt)

        response = make_response(fg.rss_str())
        response.headers.set('Content-Type', 'application/rss+xml')
        return response

    @app.route("/", methods=['GET'])
    @login_required
    def index():
        from changedetectionio import forms

        limit_tag = request.args.get('tag')
        pause_uuid = request.args.get('pause')

        # Redirect for the old rss path which used the /?rss=true
        if request.args.get('rss'):
            return redirect(url_for('rss', tag=limit_tag))

        if pause_uuid:
            try:
                datastore.data['watching'][pause_uuid]['paused'] ^= True
                datastore.needs_write = True

                return redirect(url_for('index', tag = limit_tag))
            except KeyError:
                pass

        # Sort by last_changed and add the uuid which is usually the key..
        sorted_watches = []
        for uuid, watch in datastore.data['watching'].items():

            if limit_tag != None:
                # Support for comma separated list of tags.
                for tag_in_watch in watch['tag'].split(','):
                    tag_in_watch = tag_in_watch.strip()
                    if tag_in_watch == limit_tag:
                        watch['uuid'] = uuid
                        sorted_watches.append(watch)

            else:
                watch['uuid'] = uuid
                sorted_watches.append(watch)

        sorted_watches.sort(key=lambda x: x['last_changed'], reverse=True)

        existing_tags = datastore.get_all_tags()

        form = forms.quickWatchForm(request.form)

        output = render_template("watch-overview.html",
                                 form=form,
                                 watches=sorted_watches,
                                 tags=existing_tags,
                                 active_tag=limit_tag,
                                 app_rss_token=datastore.data['settings']['application']['rss_access_token'],
                                 has_unviewed=datastore.data['has_unviewed'],
                                 # Don't link to hosting when we're on the hosting environment
                                 hosted_sticky=os.getenv("SALTED_PASS", False) == False,
                                 guid=datastore.data['app_guid'],
                                 queued_uuids=update_q.queue)
        if session.get('share-link'):
            del(session['share-link'])
        return output


    # AJAX endpoint for sending a test
    @app.route("/notification/send-test", methods=['POST'])
    @login_required
    def ajax_callback_send_notification_test():

        import apprise
        apobj = apprise.Apprise()

        # validate URLS
        if not len(request.form['notification_urls'].strip()):
            return make_response({'error': 'No Notification URLs set'}, 400)

        for server_url in request.form['notification_urls'].splitlines():
            if len(server_url.strip()):
                if not apobj.add(server_url):
                    message = '{} is not a valid AppRise URL.'.format(server_url)
                    return make_response({'error': message}, 400)

        try:
            n_object = {'watch_url': request.form['window_url'],
                        'notification_urls': request.form['notification_urls'].splitlines(),
                        'notification_title': request.form['notification_title'].strip(),
                        'notification_body': request.form['notification_body'].strip(),
                        'notification_format': request.form['notification_format'].strip()
                        }
            notification_q.put(n_object)
        except Exception as e:
            return make_response({'error': str(e)}, 400)

        return 'OK'

    @app.route("/scrub", methods=['GET', 'POST'])
    @login_required
    def scrub_page():

        if request.method == 'POST':
            confirmtext = request.form.get('confirmtext')

            if confirmtext == 'scrub':
                changes_removed = 0
                for uuid in datastore.data['watching'].keys():
                    datastore.scrub_watch(uuid)

                flash("Cleared all snapshot history")
            else:
                flash('Incorrect confirmation text.', 'error')

            return redirect(url_for('index'))

        output = render_template("scrub.html")
        return output

    def insert_watcher_entry(obj):
        ifScreenshotExist = WebScreenshots.query.filter_by(watcher_id=obj.watcher_id).order_by(id,'desc')

        if ifScreenshotExist:
            print(ifScreenshotExist)

    # If they edited an existing watch, we need to know to reset the current/previous md5 to include
    # the excluded text.
    def get_current_checksum_include_ignore_text(uuid):

        import hashlib

        from changedetectionio import fetch_site_status

        # Get the most recent one
        newest_history_key = datastore.get_val(uuid, 'newest_history_key')

        # 0 means that theres only one, so that there should be no 'unviewed' history available
        if newest_history_key == 0:
            newest_history_key = list(datastore.data['watching'][uuid]['history'].keys())[0]

        if newest_history_key:
            with open(datastore.data['watching'][uuid]['history'][newest_history_key],
                      encoding='utf-8') as file:
                raw_content = file.read()

                handler = fetch_site_status.perform_site_check(datastore=datastore)
                stripped_content = html_tools.strip_ignore_text(raw_content,
                                                             datastore.data['watching'][uuid]['ignore_text'])

                if datastore.data['settings']['application'].get('ignore_whitespace', False):
                    checksum = hashlib.md5(stripped_content.translate(None, b'\r\n\t ')).hexdigest()
                else:
                    checksum = hashlib.md5(stripped_content).hexdigest()

                return checksum

        return datastore.data['watching'][uuid]['previous_md5']


    @app.route("/edit/<string:uuid>", methods=['GET', 'POST'])
    @login_required
    # https://stackoverflow.com/questions/42984453/wtforms-populate-form-with-data-if-data-exists
    # https://wtforms.readthedocs.io/en/3.0.x/forms/#wtforms.form.Form.populate_obj ?

    def edit_page(uuid):
        from changedetectionio import forms

        using_default_check_time = True
        # More for testing, possible to return the first/only
        if not datastore.data['watching'].keys():
            flash("No watches to edit", "error")
            return redirect(url_for('index'))

        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        if not uuid in datastore.data['watching']:
            flash("No watch with the UUID %s found." % (uuid), "error")
            return redirect(url_for('index'))

        # be sure we update with a copy instead of accidently editing the live object by reference
        default = deepcopy(datastore.data['watching'][uuid])

        # Show system wide default if nothing configured
        if datastore.data['watching'][uuid]['fetch_backend'] is None:
            default['fetch_backend'] = datastore.data['settings']['application']['fetch_backend']

        # Show system wide default if nothing configured
        if all(value == 0 or value == None for value in datastore.data['watching'][uuid]['time_between_check'].values()):
            default['time_between_check'] = deepcopy(datastore.data['settings']['requests']['time_between_check'])

        # Defaults for proxy choice
        if datastore.proxy_list is not None:  # When enabled
            system_proxy = datastore.data['settings']['requests']['proxy']
            if default['proxy'] is None:
                default['proxy'] = system_proxy
            else:
                # Does the chosen one exist?
                if not any(default['proxy'] in tup for tup in datastore.proxy_list):
                    default['proxy'] = datastore.proxy_list[0][0]

            # Used by the form handler to keep or remove the proxy settings
            default['proxy_list'] = datastore.proxy_list

        # proxy_override set to the json/text list of the items
        form = forms.watchForm(formdata=request.form if request.method == 'POST' else None,
                               data=default,
                               )

        if datastore.proxy_list is None:
            # @todo - Couldn't get setattr() etc dynamic addition working, so remove it instead
            del form.proxy
        else:
            form.proxy.choices = datastore.proxy_list
            if default['proxy'] is None:
                form.proxy.default='http://hello'

        if request.method == 'POST' and form.validate():
            extra_update_obj = {}

            # Re #110, if they submit the same as the default value, set it to None, so we continue to follow the default
            # Assume we use the default value, unless something relevant is different, then use the form value
            # values could be None, 0 etc.
            # Set to None unless the next for: says that something is different
            extra_update_obj['time_between_check'] = dict.fromkeys(form.time_between_check.data)
            for k, v in form.time_between_check.data.items():
                if v and v != datastore.data['settings']['requests']['time_between_check'][k]:
                    extra_update_obj['time_between_check'] = form.time_between_check.data
                    using_default_check_time = False
                    break

            # Use the default if its the same as system wide
            if form.fetch_backend.data == datastore.data['settings']['application']['fetch_backend']:
                extra_update_obj['fetch_backend'] = None

            # Notification URLs
            datastore.data['watching'][uuid]['notification_urls'] = form.notification_urls.data

            # Ignore text
            form_ignore_text = form.ignore_text.data
            datastore.data['watching'][uuid]['ignore_text'] = form_ignore_text

            # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
            if form_ignore_text:
                if len(datastore.data['watching'][uuid]['history']):
                    extra_update_obj['previous_md5'] = get_current_checksum_include_ignore_text(uuid=uuid)

            # Reset the previous_md5 so we process a new snapshot including stripping ignore text.
            if form.css_filter.data.strip() != datastore.data['watching'][uuid]['css_filter']:
                if len(datastore.data['watching'][uuid]['history']):
                    extra_update_obj['previous_md5'] = get_current_checksum_include_ignore_text(uuid=uuid)

            datastore.data['watching'][uuid].update(form.data)
            datastore.data['watching'][uuid].update(extra_update_obj)

            flash("Updated watch.")

            # Re #286 - We wait for syncing new data to disk in another thread every 60 seconds
            # But in the case something is added we should save straight away
            datastore.needs_write_urgent = True

            # Queue the watch for immediate recheck
            update_q.put(uuid)

            # Diff page [edit] link should go back to diff page
            if request.args.get("next") and request.args.get("next") == 'diff' and not form.save_and_preview_button.data:
                return redirect(url_for('diff_history_page', uuid=uuid))
            else:
                if form.save_and_preview_button.data:
                    flash('You may need to reload this page to see the new content.')
                    return redirect(url_for('preview_page', uuid=uuid))
                else:
                    return redirect(url_for('index'))

        else:
            if request.method == 'POST' and not form.validate():
                flash("An error occurred, please see below.", "error")


            output = render_template("edit.html",
                                     uuid=uuid,
                                     watch=datastore.data['watching'][uuid],
                                     form=form,
                                     has_empty_checktime=using_default_check_time,
                                     current_base_url=datastore.data['settings']['application']['base_url'],
                                     emailprefix=os.getenv('NOTIFICATION_MAIL_BUTTON_PREFIX', False)
                                     )

        return output

    @app.route("/settings", methods=['GET', "POST"])
    @login_required
    def settings_page():
        from changedetectionio import content_fetcher, forms

        default = deepcopy(datastore.data['settings'])
        if datastore.proxy_list is not None:
            # When enabled
            system_proxy = datastore.data['settings']['requests']['proxy']
            # In the case it doesnt exist anymore
            if not any([system_proxy in tup for tup in datastore.proxy_list]):
                system_proxy = None

            default['requests']['proxy'] = system_proxy if system_proxy is not None else datastore.proxy_list[0][0]
            # Used by the form handler to keep or remove the proxy settings
            default['proxy_list'] = datastore.proxy_list


        # Don't use form.data on POST so that it doesnt overrid the checkbox status from the POST status
        form = forms.globalSettingsForm(formdata=request.form if request.method == 'POST' else None,
                                        data=default
                                        )
        if datastore.proxy_list is None:
            # @todo - Couldn't get setattr() etc dynamic addition working, so remove it instead
            del form.requests.form.proxy
        else:
            form.requests.form.proxy.choices = datastore.proxy_list

        if request.method == 'POST':
            # Password unset is a GET, but we can lock the session to a salted env password to always need the password
            if form.application.form.data.get('removepassword_button', False):
                # SALTED_PASS means the password is "locked" to what we set in the Env var
                if not os.getenv("SALTED_PASS", False):
                    datastore.remove_password()
                    flash("Password protection removed.", 'notice')
                    flask_login.logout_user()
                    return redirect(url_for('settings_page'))

            if form.validate():
                datastore.data['settings']['application'].update(form.data['application'])
                datastore.data['settings']['requests'].update(form.data['requests'])

                if not os.getenv("SALTED_PASS", False) and len(form.application.form.password.encrypted_password):
                    datastore.data['settings']['application']['password'] = form.application.form.password.encrypted_password
                    datastore.needs_write_urgent = True
                    flash("Password protection enabled.", 'notice')
                    flask_login.logout_user()
                    return redirect(url_for('index'))

                datastore.needs_write_urgent = True
                flash("Settings updated.")

            else:
                flash("An error occurred, please see below.", "error")

        output = render_template("settings.html",
                                 form=form,
                                 current_base_url = datastore.data['settings']['application']['base_url'],
                                 hide_remove_pass=os.getenv("SALTED_PASS", False),
                                 emailprefix=os.getenv('NOTIFICATION_MAIL_BUTTON_PREFIX', False))

        return output

    @app.route("/import", methods=['GET', "POST"])
    @login_required
    def import_page():
        import validators
        remaining_urls = []

        good = 0

        if request.method == 'POST':
            now=time.time()
            urls = request.values.get('urls').split("\n")

            if (len(urls) > 5000):
                flash("Importing 5,000 of the first URLs from your list, the rest can be imported again.")

            for url in urls:
                url = url.strip()
                url, *tags = url.split(" ")
                # Flask wtform validators wont work with basic auth, use validators package
                # Up to 5000 per batch so we dont flood the server
                if len(url) and validators.url(url.replace('source:', '')) and good < 5000:
                    new_uuid = datastore.add_watch(url=url.strip(), tag=" ".join(tags), write_to_disk_now=False)
                    if new_uuid:
                        # Straight into the queue.
                        update_q.put(new_uuid)
                        good += 1
                        continue

                if len(url.strip()):
                    remaining_urls.append(url)

            flash("{} Imported in {:.2f}s, {} Skipped.".format(good, time.time()-now,len(remaining_urls)))
            datastore.needs_write = True

            if len(remaining_urls) == 0:
                # Looking good, redirect to index.
                return redirect(url_for('index'))

        # Could be some remaining, or we could be on GET
        output = render_template("import.html",
                                 remaining="\n".join(remaining_urls)
                                 )
        return output

    # Clear all statuses, so we do not see the 'unviewed' class
    @app.route("/api/mark-all-viewed", methods=['GET'])
    @login_required
    def mark_all_viewed():

        # Save the current newest history as the most recently viewed
        for watch_uuid, watch in datastore.data['watching'].items():
            datastore.set_last_viewed(watch_uuid, watch['newest_history_key'])

        flash("Cleared all statuses.")
        return redirect(url_for('index'))

    @app.route("/diff/<string:uuid>", methods=['GET'])
    @login_required
    def diff_history_page(uuid):

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        extra_stylesheets = [url_for('static_content', group='styles', filename='diff.css')]
        try:
            watch = datastore.data['watching'][uuid]
        except KeyError:
            flash("No history found for the specified link, bad link?", "error")
            return redirect(url_for('index'))

        dates = list(watch['history'].keys())
        # Convert to int, sort and back to str again
        # @todo replace datastore getter that does this automatically
        dates = [int(i) for i in dates]
        dates.sort(reverse=True)
        dates = [str(i) for i in dates]

        if len(dates) < 2:
            flash("Not enough saved change detection snapshots to produce a report.", "error")
            return redirect(url_for('index'))

        # Save the current newest history as the most recently viewed
        datastore.set_last_viewed(uuid, dates[0])
        newest_file = watch['history'][dates[0]]

        try:
            with open(newest_file, 'r') as f:
                newest_version_file_contents = f.read()
        except Exception as e:
            newest_version_file_contents = "Unable to read {}.\n".format(newest_file)

        previous_version = request.args.get('previous_version')
        try:
            previous_file = watch['history'][previous_version]
        except KeyError:
            # Not present, use a default value, the second one in the sorted list.
            previous_file = watch['history'][dates[1]]

        try:
            with open(previous_file, 'r') as f:
                previous_version_file_contents = f.read()
        except Exception as e:
            previous_version_file_contents = "Unable to read {}.\n".format(previous_file)


        screenshot_url = datastore.get_screenshot(uuid)

        output = render_template("diff.html", watch_a=watch,
                                 newest=newest_version_file_contents,
                                 previous=previous_version_file_contents,
                                 extra_stylesheets=extra_stylesheets,
                                 versions=dates[1:],
                                 uuid=uuid,
                                 newest_version_timestamp=dates[0],
                                 current_previous_version=str(previous_version),
                                 current_diff_url=watch['url'],
                                 extra_title=" - Diff - {}".format(watch['title'] if watch['title'] else watch['url']),
                                 left_sticky=True,
                                 screenshot=screenshot_url)

        return output

    @app.route("/preview/<string:uuid>", methods=['GET'])
    @login_required
    def preview_page(uuid):
        content = []
        ignored_line_numbers = []
        trigger_line_numbers = []

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        extra_stylesheets = [url_for('static_content', group='styles', filename='diff.css')]

        try:
            watch = datastore.data['watching'][uuid]
        except KeyError:
            flash("No history found for the specified link, bad link?", "error")
            return redirect(url_for('index'))

        if len(watch['history']):
            timestamps = sorted(watch['history'].keys(), key=lambda x: int(x))
            filename = watch['history'][timestamps[-1]]
            try:
                with open(filename, 'r') as f:
                    tmp = f.readlines()

                    # Get what needs to be highlighted
                    ignore_rules = watch.get('ignore_text', []) + datastore.data['settings']['application']['global_ignore_text']

                    # .readlines will keep the \n, but we will parse it here again, in the future tidy this up
                    ignored_line_numbers = html_tools.strip_ignore_text(content="".join(tmp),
                                                                        wordlist=ignore_rules,
                                                                        mode='line numbers'
                                                                        )

                    trigger_line_numbers = html_tools.strip_ignore_text(content="".join(tmp),
                                                                        wordlist=watch['trigger_text'],
                                                                        mode='line numbers'
                                                                        )
                    # Prepare the classes and lines used in the template
                    i=0
                    for l in tmp:
                        classes=[]
                        i+=1
                        if i in ignored_line_numbers:
                            classes.append('ignored')
                        if i in trigger_line_numbers:
                            classes.append('triggered')
                        content.append({'line': l, 'classes': ' '.join(classes)})


            except Exception as e:
                content.append({'line': "File doesnt exist or unable to read file {}".format(filename), 'classes': ''})
        else:
            content.append({'line': "No history found", 'classes': ''})

        screenshot_url = datastore.get_screenshot(uuid)
        output = render_template("preview.html",
                                 content=content,
                                 extra_stylesheets=extra_stylesheets,
                                 ignored_line_numbers=ignored_line_numbers,
                                 triggered_line_numbers=trigger_line_numbers,
                                 current_diff_url=watch['url'],
                                 screenshot=screenshot_url,
                                 watch=watch,
                                 uuid=uuid)
        
        return output

    @app.route("/settings/notification-logs", methods=['GET'])
    @login_required
    def notification_logs():
        global notification_debug_log
        output = render_template("notification-log.html",
                                 logs=notification_debug_log if len(notification_debug_log) else ["No errors or warnings detected"])

        return output

    @app.route("/api/<string:uuid>/snapshot/current", methods=['GET'])
    @login_required
    def api_snapshot(uuid):

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        try:
            watch = datastore.data['watching'][uuid]
        except KeyError:
            return abort(400, "No history found for the specified link, bad link?")

        newest = list(watch['history'].keys())[-1]
        with open(watch['history'][newest], 'r') as f:
            content = f.read()

        resp = make_response(content)
        resp.headers['Content-Type'] = 'text/plain'
        return resp

    @app.route("/favicon.ico", methods=['GET'])
    def favicon():
        return send_from_directory("static/images", path="favicon.ico")

    # We're good but backups are even better!
    @app.route("/backup", methods=['GET'])
    @login_required
    def get_backup():

        import zipfile
        from pathlib import Path

        # Remove any existing backup file, for now we just keep one file

        for previous_backup_filename in Path(datastore_o.datastore_path).rglob('changedetection-backup-*.zip'):
            os.unlink(previous_backup_filename)

        # create a ZipFile object
        backupname = "changedetection-backup-{}.zip".format(int(time.time()))

        # We only care about UUIDS from the current index file
        uuids = list(datastore.data['watching'].keys())
        backup_filepath = os.path.join(datastore_o.datastore_path, backupname)

        with zipfile.ZipFile(backup_filepath, "w",
                             compression=zipfile.ZIP_DEFLATED,
                             compresslevel=8) as zipObj:

            # Be sure we're written fresh
            datastore.sync_to_json()

            # Add the index
            zipObj.write(os.path.join(datastore_o.datastore_path, "url-watches.json"), arcname="url-watches.json")

            # Add the flask app secret
            zipObj.write(os.path.join(datastore_o.datastore_path, "secret.txt"), arcname="secret.txt")

            # Add any snapshot data we find, use the full path to access the file, but make the file 'relative' in the Zip.
            for txt_file_path in Path(datastore_o.datastore_path).rglob('*.txt'):
                parent_p = txt_file_path.parent
                if parent_p.name in uuids:
                    zipObj.write(txt_file_path,
                                 arcname=str(txt_file_path).replace(datastore_o.datastore_path, ''),
                                 compress_type=zipfile.ZIP_DEFLATED,
                                 compresslevel=8)

            # Create a list file with just the URLs, so it's easier to port somewhere else in the future
            list_file = "url-list.txt"
            with open(os.path.join(datastore_o.datastore_path, list_file), "w") as f:
                for uuid in datastore.data["watching"]:
                    url = datastore.data["watching"][uuid]["url"]
                    f.write("{}\r\n".format(url))
            list_with_tags_file = "url-list-with-tags.txt"
            with open(
                os.path.join(datastore_o.datastore_path, list_with_tags_file), "w"
            ) as f:
                for uuid in datastore.data["watching"]:
                    url = datastore.data["watching"][uuid]["url"]
                    tag = datastore.data["watching"][uuid]["tag"]
                    f.write("{} {}\r\n".format(url, tag))

            # Add it to the Zip
            zipObj.write(
                os.path.join(datastore_o.datastore_path, list_file),
                arcname=list_file,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=8,
            )
            zipObj.write(
                os.path.join(datastore_o.datastore_path, list_with_tags_file),
                arcname=list_with_tags_file,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=8,
            )

        # Send_from_directory needs to be the full absolute path
        return send_from_directory(os.path.abspath(datastore_o.datastore_path), backupname, as_attachment=True)

    @app.route("/static/<string:group>/<string:filename>", methods=['GET'])
    def static_content(group, filename):
        if group == 'screenshot':

            from flask import make_response

            # Could be sensitive, follow password requirements
            if datastore.data['settings']['application']['password'] and not flask_login.current_user.is_authenticated:
                abort(403)

            # These files should be in our subdirectory
            try:
                # set nocache, set content-type
                watch_dir = datastore_o.datastore_path + "/" + filename
                response = make_response(send_from_directory(filename="last-screenshot.png", directory=watch_dir, path=watch_dir + "/last-screenshot.png"))
                response.headers['Content-type'] = 'image/png'
                response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = 0
                return response

            except FileNotFoundError:
                abort(404)

        # These files should be in our subdirectory
        try:
            return send_from_directory("static/{}".format(group), path=filename)
        except FileNotFoundError:
            abort(404)

    @app.route("/api/add", methods=['POST'])
    @login_required
    def api_watch_add():
        from changedetectionio import forms
        form = forms.quickWatchForm(request.form)

        if not form.validate():
            flash("Error")
            return redirect(url_for('index'))

        url = request.form.get('url').strip()
        if datastore.url_exists(url):
            flash('The URL {} already exists'.format(url), "error")
            return redirect(url_for('index'))

        # @todo add_watch should throw a custom Exception for validation etc
        new_uuid = datastore.add_watch(url=url, tag=request.form.get('tag').strip())
        print(new_uuid)
        if new_uuid:
            # Straight into the queue.
            update_q.put(new_uuid)
            flash("Watch added.")

        return redirect(url_for('index'))



    @app.route("/api/delete", methods=['GET'])
    @login_required
    def api_delete():
        uuid = request.args.get('uuid')

        if uuid != 'all' and not uuid in datastore.data['watching'].keys():
            flash('The watch by UUID {} does not exist.'.format(uuid), 'error')
            return redirect(url_for('index'))

        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()
        datastore.delete(uuid)
        flash('Deleted.')

        return redirect(url_for('index'))

    @app.route("/api/clone", methods=['GET'])
    @login_required
    def api_clone():
        uuid = request.args.get('uuid')
        # More for testing, possible to return the first/only
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        new_uuid = datastore.clone(uuid)
        update_q.put(new_uuid)
        flash('Cloned.')

        return redirect(url_for('index'))

    @app.route("/api/checknow", methods=['GET'])
    @login_required
    def api_watch_checknow():

        tag = request.args.get('tag')
        uuid = request.args.get('uuid')
        i = 0

        running_uuids = []
        for t in running_update_threads:
            running_uuids.append(t.current_uuid)

        # @todo check thread is running and skip

        if uuid:
            if uuid not in running_uuids:
                update_q.put(uuid)
            i = 1

        elif tag != None:
            # Items that have this current tag
            for watch_uuid, watch in datastore.data['watching'].items():
                if (tag != None and tag in watch['tag']):
                    if watch_uuid not in running_uuids and not datastore.data['watching'][watch_uuid]['paused']:
                        update_q.put(watch_uuid)
                        i += 1

        else:
            # No tag, no uuid, add everything.
            for watch_uuid, watch in datastore.data['watching'].items():

                if watch_uuid not in running_uuids and not datastore.data['watching'][watch_uuid]['paused']:
                    update_q.put(watch_uuid)
                    i += 1
        flash("{} watches are queued for rechecking.".format(i))
        return redirect(url_for('index', tag=tag))

    @app.route("/api/share-url", methods=['GET'])
    @login_required
    def api_share_put_watch():
        """Given a watch UUID, upload the info and return a share-link
           the share-link can be imported/added"""
        import requests
        import json
        tag = request.args.get('tag')
        uuid = request.args.get('uuid')

        # more for testing
        if uuid == 'first':
            uuid = list(datastore.data['watching'].keys()).pop()

        # copy it to memory as trim off what we dont need (history)
        watch = deepcopy(datastore.data['watching'][uuid])
        if (watch.get('history')):
            del (watch['history'])

        # for safety/privacy
        for k in list(watch.keys()):
            if k.startswith('notification_'):
                del watch[k]

        for r in['uuid', 'last_checked', 'last_changed']:
            if watch.get(r):
                del (watch[r])

        # Add the global stuff which may have an impact
        watch['ignore_text'] += datastore.data['settings']['application']['global_ignore_text']
        watch['subtractive_selectors'] += datastore.data['settings']['application']['global_subtractive_selectors']

        watch_json = json.dumps(watch)

        try:
            r = requests.request(method="POST",
                                 data={'watch': watch_json},
                                 url="https://changedetection.io/share/share",
                                 headers={'App-Guid': datastore.data['app_guid']})
            res = r.json()

            session['share-link'] = "https://changedetection.io/share/{}".format(res['share_key'])


        except Exception as e:
            flash("Could not share, something went wrong while communicating with the share server.", 'error')

        # https://changedetection.io/share/VrMv05wpXyQa
        # in the browser - should give you a nice info page - wtf
        # paste in etc
        return redirect(url_for('index'))


    # @todo handle ctrl break
    ticker_thread = threading.Thread(target=ticker_thread_check_time_launch_checks).start()

    threading.Thread(target=notification_runner).start()

    # Check for new release version, but not when running in test/build
    if not os.getenv("GITHUB_REF", False):
        threading.Thread(target=check_for_new_version).start()

    return app


# Check for new version and anonymous stats
def check_for_new_version():
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    while not app.config.exit.is_set():
        try:
            r = requests.post("https://changedetection.io/check-ver.php",
                              data={'version': __version__,
                                    'app_guid': datastore.data['app_guid'],
                                    'watch_count': len(datastore.data['watching'])
                                    },

                              verify=False)
        except:
            pass

        try:
            if "new_version" in r.text:
                app.config['NEW_VERSION_AVAILABLE'] = True
        except:
            pass

        # Check daily
        app.config.exit.wait(86400)

def notification_runner():
    global notification_debug_log
    while not app.config.exit.is_set():
        try:
            # At the moment only one thread runs (single runner)
            n_object = notification_q.get(block=False)
        except queue.Empty:
            time.sleep(1)

        else:
            # Process notifications
            try:
                from changedetectionio import notification
                notification.process_notification(n_object, datastore)

            except Exception as e:
                print("Watch URL: {}  Error {}".format(n_object['watch_url'], str(e)))

                # UUID wont be present when we submit a 'test' from the global settings
                if 'uuid' in n_object:
                    datastore.update_watch(uuid=n_object['uuid'],
                                           update_obj={'last_notification_error': "Notification error detected, please see logs."})

                log_lines = str(e).splitlines()
                notification_debug_log += log_lines

                # Trim the log length
                notification_debug_log = notification_debug_log[-100:]


# Thread runner to check every minute, look for new watches to feed into the Queue.
def ticker_thread_check_time_launch_checks():
    from changedetectionio import update_worker

    # Spin up Workers that do the fetching
    # Can be overriden by ENV or use the default settings
    n_workers = int(os.getenv("FETCH_WORKERS", datastore.data['settings']['requests']['workers']))
    for _ in range(n_workers):
        new_worker = update_worker.update_worker(update_q, notification_q, app, datastore)
        running_update_threads.append(new_worker)
        new_worker.start()

    while not app.config.exit.is_set():

        # Get a list of watches by UUID that are currently fetching data
        running_uuids = []
        for t in running_update_threads:
            if t.current_uuid:
                running_uuids.append(t.current_uuid)

        # Re #232 - Deepcopy the data incase it changes while we're iterating through it all
        while True:
            try:
                copied_datastore = deepcopy(datastore)
            except RuntimeError as e:
                # RuntimeError: dictionary changed size during iteration
                time.sleep(0.1)
            else:
                break

        # Re #438 - Don't place more watches in the queue to be checked if the queue is already large
        while update_q.qsize() >= 2000:
            time.sleep(1)

        # Check for watches outside of the time threshold to put in the thread queue.
        now = time.time()

        recheck_time_minimum_seconds = int(os.getenv('MINIMUM_SECONDS_RECHECK_TIME', 60))
        recheck_time_system_seconds = datastore.threshold_seconds

        for uuid, watch in copied_datastore.data['watching'].items():

            # No need todo further processing if it's paused
            if watch['paused']:
                continue

            # If they supplied an individual entry minutes to threshold.
            threshold = now
            watch_threshold_seconds = watch.threshold_seconds()
            if watch_threshold_seconds:
                threshold -= watch_threshold_seconds
            else:
                threshold -= recheck_time_system_seconds

            # Yeah, put it in the queue, it's more than time
            if watch['last_checked'] <= max(threshold, recheck_time_minimum_seconds):
                if not uuid in running_uuids and uuid not in update_q.queue:
                    update_q.put(uuid)

        # Wait a few seconds before checking the list again
        time.sleep(3)

        # Should be low so we can break this out in testing
        app.config.exit.wait(1)