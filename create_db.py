from changedetectionio import db

if __name__ == '__main__':
    print('Creating DB')
    db.create_all()
    # p = WebScreenshots(watcher_id='dummy', file_name_txt_file='dummy', image_name_with_mark='dummy', image_name_without_mark='dummy')
    # db.session.add(p)
    # db.session.commit()
    