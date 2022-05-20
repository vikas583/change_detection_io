from enum import unique
from pickle import TRUE
from time import timezone
from changedetectionio import db
from sqlalchemy.sql import func

class WebScreenshots(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url: db.Column(db.Text, unique=True)
    watcher_id = db.Column(db.String(255))
    file_name_txt_file = db.Column(db.String(255))
    image_name_with_mark = db.Column(db.String(255))
    image_name_without_mark = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True),server_default=func.now())
