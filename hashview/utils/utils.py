import os
import secrets
import hashlib
import hashlib
import time
import _md5
import re
from datetime import datetime
from hashview.models import db
from hashview.models import Rules, Wordlists, Hashfiles, HashfileHashes, Hashes, Tasks, Jobs, JobTasks, JobNotifications, Users, Agents
from flask_mail import Message
from flask import current_app, url_for
import requests


def save_file(path, form_file):
    random_hex = secrets.token_hex(8)
    file_name = random_hex + os.path.split(form_file.filename)[0] + '.txt'
    file_path = os.path.join(current_app.root_path, path, file_name)
    form_file.save(file_path)
    return file_path

def _count_generator(reader):
    b = reader(1024 * 1024)
    while b:
        yield b
        b = reader(1024 * 1024)

def get_linecount(filepath):

    with open(filepath, 'rb') as fp:
        c_generator = _count_generator(fp.raw.read)
        count = sum(buffer.count(b'\n') for buffer in c_generator)
        return count + 1

def get_filehash(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath,"rb") as f:
        # Read and update hash string value in blocks of 4K
        for byte_block in iter(lambda: f.read(4096),b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def send_email(user, subject, message):
    msg = Message(subject, recipients=[user.email_address])
    msg.body = message
    current_app.extensions['mail'].send(msg)

def send_html_email(user, subject, message):
    msg = Message(subject, recipients=[user.email_address])
    msg.html = message
    current_app.extensions['mail'].send(msg)

def send_pushover(user, subject, message):
    if not user.pushover_user_key:
        current_app.logger.info('SendPushover is Complete with Failure(User Key not Configured).')
        return

    if not user.pushover_app_id:
        current_app.logger.info('SendPushover is Complete with Failure(App Id not Configured).')
        return

    # https://pushover.net/api
    payload = dict(
        token   = user.pushover_app_id,
        user    = user.pushover_user_key,
        message = message,
        title   = subject,
    )
    response = requests.post('https://api.pushover.net/1/messages.json', params=payload)
    response_json = response.json()
    if 400 <= response.status_code < 500:
        current_app.logger.info('SendPushover is Complete with Failure(%s).', response_json.get('errors'))
        send_email(user, 'Error Sending Push Notification', f'Check your Pushover API keys in  your profile. Original Message: {message}')
        return

    else:
        current_app.logger.info('SendPushover is Complete with Success(%s).', response_json)
        return

def get_md5_hash(string):
    #m = hashlib.md5()
    #m.update(string.encode('utf-8'))
    m = _md5.md5(string.encode('utf-8'))
    return m.hexdigest()

def import_hash_only(line, hash_type):
    hash = Hashes.query.filter_by(hash_type=hash_type, sub_ciphertext=get_md5_hash(line)).first()
    if hash:
        return hash.id
    else:
        new_hash = Hashes(hash_type=hash_type, sub_ciphertext=get_md5_hash(line), ciphertext=line, cracked=0)
        db.session.add(new_hash)
        db.session.commit()
        return new_hash.id

def import_hashfilehashes(hashfile_id, hashfile_path, file_type, hash_type):
    # Open file
    file = open(hashfile_path, 'r')
    lines = file.readlines()

    # for line in file,
    for line in lines:
        # If line is empty:
        if len(line) > 0:
            if file_type == 'hash_only':
                # forcing lower casing of hash as hashcat will return lower cased version of the has and we want to match what we imported.
                if hash_type == '300':
                    hash_id = import_hash_only(line=line.lower().rstrip(), hash_type=hash_type)
                else:
                    hash_id = import_hash_only(line=line.rstrip(), hash_type=hash_type)
                if hash_type == '2100':
                    username = line.split('#')[1]
                else:
                    username = None
            elif file_type == 'user_hash':
                hash_id = import_hash_only(line=line.split(':',1)[1].rstrip(), hash_type=hash_type)
                username = line.split(':')[0]
            elif file_type == 'shadow':
                hash_id= import_hash_only(line=line.split(':')[1], hash_type=hash_type)
                username = line.split(':')[0]
            elif file_type == 'pwdump':
                # do we let user select LM so that we crack those instead of NTLM?
                # First extracting usernames so we can filter out machine accounts
                if re.search("\$$", line.split(':')[0]):
                #if '$' in line.split(':')[0]:
                    continue
                else:
                    hash_id = import_hash_only(line=line.split(':')[3].lower(), hash_type='1000')
                    username = line.split(':')[0]
            elif file_type == 'kerberos':
                hash_id = import_hash_only(line=line.lower().rstrip(), hash_type=hash_type)
                if hash_type == '18200':
                    username = line.split('$')[3].split(':')[0]
                else:
                    username = line.split('$')[3]
            elif file_type == 'NetNTLM':
                # First extracting usernames so we can filter out machine accounts
                # 5600, domain is case sensitve. Hashcat returns username in upper case.
                if re.search("\$$", line.split(':')[0]):
                #if '$' in line.split(':')[0]:
                    continue
                else:
                    # uppercase uesrname in line
                    line_list = line.split(':')
                    # uppercase the username in line
                    line_list[0] = line_list[0].upper()
                    # lowercase the rest (except domain name) 3,4,5
                    line_list[3] = line_list[3].lower()
                    line_list[4] = line_list[4].lower()
                    line_list[5] = line_list[5].lower()
                    line = ':'.join(line_list)
                    hash_id = import_hash_only(line=line.rstrip(), hash_type=hash_type)
                    username = line.split(':')[0]
            else:
                return False
            if username == None:
                hashfilehashes = HashfileHashes(hash_id=hash_id, hashfile_id=hashfile_id)
            else:
                hashfilehashes = HashfileHashes(hash_id=hash_id, username=username.encode('latin-1').hex(), hashfile_id=hashfile_id)
            db.session.add(hashfilehashes)
            db.session.commit()

    return True

def update_dynamic_wordlist(wordlist_id):
    wordlist = Wordlists.query.get(wordlist_id)
    hashes = Hashes.query.filter_by(cracked=True).distinct('plaintext')

    # Do we delete the original file, or overwrite it?
    # if we overwrite, what happens if the new content has fewer lines than the previous file.
    # would this even happen? In most/all cases there will be new stuff to add.
    # is there a file lock on a wordlist when in use by hashcat? Could we just create a temp file and replace after generation?
    # Open file
    file = open(wordlist.path, 'wt')
    for entry in hashes:
        file.write(str(bytes.fromhex(entry.plaintext).decode('latin-1')) + '\n')
    file.close()

    # update line count
    wordlist.size = get_linecount(wordlist.path)
    # update file hash
    wordlist.checksum = get_filehash(wordlist.path)
    # update last update
    wordlist.last_updated = datetime.today()
    db.session.commit()

def build_hashcat_command(job_id, task_id):
    # this function builds the main hashcat cmd we use to crack.
    hc_binpath = '@HASHCATBINPATH@'
    task = Tasks.query.get(task_id)
    job = Jobs.query.get(job_id)
    rules_file = Rules.query.get(task.rule_id)
    hashfilehashes_single_entry = HashfileHashes.query.filter_by(hashfile_id = job.hashfile_id).first()
    hashes_single_entry = Hashes.query.get(hashfilehashes_single_entry.hash_id)
    hash_type = hashes_single_entry.hash_type
    attackmode = task.hc_attackmode
    mask = task.hc_mask

    if attackmode == 'combinator':
        print('unsupported combinator')
    else:
        wordlist = Wordlists.query.get(task.wl_id)

    target_file = 'control/hashes/hashfile_' + str(job.id) + '_' + str(task.id) + '.txt'
    crack_file = 'control/outfiles/hc_cracked_' + str(job.id) + '_' + str(task.id) + '.txt'
    if wordlist:
        relative_wordlist_path = 'control/wordlists/' + wordlist.path.split('/')[-1]
    else:
        relative_wordlist_path = ''
    if rules_file:
        relative_rules_path = 'control/rules/' + rules_file.path.split('/')[-1]
    else:
        relative_rules_path = ''

    session = secrets.token_hex(4)

    if attackmode == 'bruteforce':
        cmd = ['-O','-w','3','--session',session,'-m',str(hash_type),'--potfile-disable','--status','--status-timer=15','--outfile-format','1,3','--outfile',crack_file,'-a','3',target_file]
        #cmd = hc_binpath + ' -O -w 3 ' + ' --session ' + session + ' -m ' + str(hash_type) + ' --potfile-disable' + ' --status --status-timer=15' + ' --outfile-format 1,3' + ' --outfile ' + crack_file + ' ' + ' -a 3 ' + target_file
    elif attackmode == 'maskmode':
        cmd = ['-O','-w','3','--session',session,'-m',str(hash_type),'--potfile-disable','--status','--status-timer=15','--outfile-format','1,3','--outfile',crack_file,'-a','3',target_file,mask]
        #cmd = hc_binpath + ' -O -w 3 ' + ' --session ' + session + ' -m ' + str(hash_type) + ' --potfile-disable' + ' --status --status-timer=15' + ' --outfile-format 1,3' + ' --outfile ' + crack_file + ' ' + ' -a 3 ' + target_file + ' ' + mask
    elif attackmode == 'dictionary':
        if isinstance(task.rule_id, int):
            cmd = ['-O','-w','3','--session',session,'-m',str(hash_type),'--potfile-disable','--status','--status-timer=15','--outfile-format','1,3','--outfile',crack_file,'-r',relative_rules_path,target_file,relative_wordlist_path]
            #cmd = hc_binpath + ' -O -w 3 ' + ' --session ' + session + ' -m ' + str(hash_type) + ' --potfile-disable' + ' --status --status-timer=15' + ' --outfile-format 1,3' + ' --outfile ' + crack_file + ' ' + ' -r ' + relative_rules_path + ' ' + target_file + ' ' + relative_wordlist_path
        else:
            cmd =['-O','-w','3','--session',session,'-m',str(hash_type),'--potfile-disable','--status','--status-timer=15','--outfile-format','1,3','--outfile',crack_file,target_file,relative_wordlist_path]
            #cmd = hc_binpath + ' -O -w 3 ' + ' --session ' + session + ' -m ' + str(hash_type) + ' --potfile-disable' + ' --status --status-timer=15' + ' --outfile-format 1,3' + ' --outfile ' + crack_file + ' ' + target_file + ' ' + relative_wordlist_path
    elif attackmode == 'combinator':
      cmd = ['-O','-w','3','--session',session,'-m',str(hash_type),'--potfile-disable','--status','--status-timer=15','--outfile-format','1,3','--outfile',crack_file,'-a','1',target_file,wordlist_one.path, wordlist_two.path,relative_rules_path]
      #cmd = hc_binpath + ' -O -w 3 ' + ' --session ' + session + ' -m ' + str(hash_type) + ' --potfile-disable' + ' --status --status-timer=15' + ' --outfile-format 1,3' + ' --outfile ' + crack_file + ' ' + ' -a 1 ' + target_file + ' ' + wordlist_one.path + ' ' + ' ' + wordlist_two.path + ' ' + relative_rules_path

    return cmd

def update_job_task_status(jobtask_id, status):

    jobtask = JobTasks.query.get(jobtask_id)

    if jobtask is None:
        return False

    jobtask.status = status
    if status == 'Completed':
        jobtask.agent_id = None
        agent = Agents.query.get(jobtask.agent_id)
        if agent:
            agent.hc_status = ''
    db.session.commit()

    # Update Jobs
    # TODO
    # Shouldn't we be changing the job stats to match the jobtask status?
    # Add started at time
    job = Jobs.query.get(jobtask.job_id)
    if job.status == 'Queued':
        job.status = 'Running'
        job.started_at = time.strftime('%Y-%m-%d %H:%M:%S')
        db.session.commit()

    # TODO
    # This is such a janky way of doing this. Instead of having the agent tell us its done, we're just assuming
    # That if no other tasks are active we must be done
    done = True
    jobtasks = JobTasks.query.filter_by(job_id=job.id).all()
    for jobtask in jobtasks:
        if jobtask.status == 'Queued' or jobtask.status == 'Running' or jobtask.status == 'Importing':
            done = False

    if done:
        job.status = 'Completed'
        job.ended_at = time.strftime('%Y-%m-%d %H:%M:%S')
        db.session.commit()

        start_time = datetime.strptime(str(job.started_at), '%Y-%m-%d %H:%M:%S')
        end_time = datetime.strptime(str(job.ended_at), '%Y-%m-%d %H:%M:%S')
        durration = (abs(end_time - start_time).seconds) # So dumb you cant conver this to minutes, only resolution is seconds or days :(

        hashfile = Hashfiles.query.get(job.hashfile_id)
        hashfile.runtime += durration
        db.session.commit()

        # TODO
        # mark all jobtasks as completed
        job_notifications = JobNotifications.query.filter_by(job_id = job.id)

        # Send Notifications
        for job_notification in job_notifications:
            user = Users.query.get(job_notification.owner_id)
            cracked_cnt = db.session.query(Hashes).outerjoin(HashfileHashes, Hashes.id==HashfileHashes.hash_id).filter(Hashes.cracked == '1').filter(HashfileHashes.hashfile_id==job.hashfile_id).count()
            uncracked_cnt = db.session.query(Hashes).outerjoin(HashfileHashes, Hashes.id==HashfileHashes.hash_id).filter(Hashes.cracked == '0').filter(HashfileHashes.hashfile_id==job.hashfile_id).count()
            if job_notification.method == 'email':
                send_html_email(user, 'Hashview Job: "' + job.name + '" Has Completed!', 'Your job has completed. It ran for ' + getTimeFormat(durration) + ' and resulted in a total of ' + str(cracked_cnt) + ' out of ' + str(cracked_cnt+uncracked_cnt) + ' hashes being recovered! <br /><br /> <a href="' + url_for('analytics.get_analytics',customer_id=job.customer_id,hashfile_id=job.hashfile_id, _external=True) + '">View Analytics</a>')
            elif job_notification.method == 'push':
                if user.pushover_user_key and user.pushover_app_id:
                    send_pushover(user, 'Message from Hashview', 'Hashview Job: "' + job.name + '" Has Completed!')
                else:
                    send_email(user, 'Hashview: Missing Pushover Key', 'Hello, you were due to recieve a pushover notification, but because your account was not provisioned with an pushover ID and Key, one could not be set. Please log into hashview and set these options under Manage->Profile.')
            db.session.delete(job_notification)
            db.session.commit()

    return True

# Dumb way of doing this, we return with an error message if we have an issue with the hashfile
# and return false if hashfile is okay. :/ Should be the otherway around :shrug emoji:
def validate_hashfile(hashfile_path, file_type, hash_type):

    file = open(hashfile_path, 'r')
    lines = file.readlines()
    line_number = 0

    # Do a whole file check if file_type is NetNTLM
    # If duplicate usernames exists return error
    if file_type == 'NetNTLM':
        list_of_username_and_computers = []
        for line in lines:
            username_computer = (line.split(':')[0] + ':' + line.split(':')[2]).lower()
            if username_computer in list_of_username_and_computers:
                return 'Error: Duplicate usernames / computer found in hashfiles (' + str(username_computer) + '). Please only submit unique usernames / computer.'
            else:
                list_of_username_and_computers.append(username_computer)

    line_number = 0
    # for line in file,
    for line in lines:
        line_number += 1
        # TODO
        # Skip entries that are just newlines
        if len(line) > 50000:
            return 'Error line ' + str(line_number) + ' is too long. Line length: ' + str(len(line)) + '. Max length is 50,000 chars.'
        if len(line) > 0:

            # Check file types & hash types
            if file_type == 'hash_only':
                if ':' in line:
                    return 'Error line ' + str(line_number) + ' contains a : character. File should be hashes only. No usernames'
                if hash_type == '0' or hash_type == '1000':
                    if len(line.rstrip()) != 32:
                        return 'Error line ' + str(line_number) + ' has an invalid number of characters (' + str(len(line.rstrip())) + ') should be 32'
                if hash_type == '300':
                    if len(line.rstrip()) != 40:
                        return 'Error line ' + str(line_number) + ' has an invalid number of characters (' + str(len(line.rstrip())) + ') should be 40'
                if hash_type == '500':
                    if '$1$' not in line:
                        return 'Error line ' + str(line_number) + ' appears to not be a valid md5Crypt hash'
                if hash_type == '2100':
                    if '$' not in line:
                        return 'Error line ' + str(line_number) + ' is missing a $ character. DCC2 Hashes should have these.'
                    dollar_cnt = 0
                    hash_cnt = 0
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                        if char == '#':
                            hash_cnt += 1
                    if dollar_cnt != 2:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: DCC2 MS Cache'
                    if hash_cnt != 2:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: DCC2 MS Cache'
                if hash_type == '1800':
                    dollar_cnt = 0
                    for char in line:
                        if char == '$':
                            dollar_cnt+=1
                    if dollar_cnt != 3:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Sha512 Crypt.'
                    if '$6$' not in line:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Sha512 Crypt.'
                if hash_type == '3200':
                    if '$' not in line:
                        return 'Error line ' + str(line_number) + ' is missing a $ character. bcrypt Hashes should have these.'
                    dollar_cnt = 0
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 3:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: bcrypt'

            if file_type == 'shadow':
                if ':' not in line:
                    return 'Error line ' + str(line_number) + ' is missing a : character. shadow file should include usernames.'
                if hash_type == '1800':
                    dollar_cnt = 0
                    for char in line:
                        if char == '$':
                            dollar_cnt+=1
                    if dollar_cnt != 3:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Sha512 Crypt from a shadow file.'
                    if '$6$' not in line:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Sha512 Crypt from a shadow file.'

            elif file_type == 'pwdump':
                if ':' not in line:
                    return 'Error line ' + str(line_number) + ' is missing a : character. Pwdump file should include usernames.'
                # This is slow af :(
                colon_cnt = 0
                for char in line:
                    if char == ':':
                        colon_cnt += 1
                if colon_cnt < 6:
                    return 'Error line ' + str(line_number) + '. File does not appear to be be in a pwdump format.'
                if hash_type == '1000':
                    if len(line.split(':')[3]) != 32:
                        return 'Error line ' + str(line_number) + ' has an invalid number of characters (' + str(len(line.rstrip())) + ') should be 32'
                else:
                    return 'Sorry. The only Hash Type we support for PWDump files is NTLM'
            elif file_type == 'kerberos':
                if '$' not in line:
                    return 'Error line ' + str(line_number) + ' is missing a $ character. kerberos file should include these.'
                if len(line) > 16384:
                    return 'Error line ' + str(line_number) + ' is too long. Max char length is 16384. If you need long please submit an issue on GitHub'
                dollar_cnt = 0
                if hash_type == '7500':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 6:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REQ Pre-Auth (1)'
                    if line.split('$')[1] != 'krb5pa':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REQ Pre-Auth (2)'
                    if line.split('$')[2] != '23':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REQ Pre-Auth (3)'
                elif hash_type == '13100':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 7 and dollar_cnt != 8:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, TGS-REP (1)'
                    if line.split('$')[1] != 'krb5tgs':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, TGS-REP (2)'
                    if line.split('$')[2] != '23':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, TGS-REP (3)'
                elif hash_type == '18200':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 4 and dollar_cnt != 5:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REP (1)'
                    if line.split('$')[1] != 'krb5asrep':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REP (2)'
                    if line.split('$')[2] != '23':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 23, AS-REP (3)'
                elif hash_type == '19600':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 6 and dollar_cnt != 7:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, TGS-REP (AES128-CTS-HMAC-SHA1-96) (1)'
                    if line.split('$')[1] != 'krb5tgs':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, TGS-REP (AES128-CTS-HMAC-SHA1-96) (2)'
                    if line.split('$')[2] != '17':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, TGS-REP (AES128-CTS-HMAC-SHA1-96) (3)'
                elif hash_type == '19700':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 6 and dollar_cnt != 7:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, TGS-REP (AES256-CTS-HMAC-SHA1-96) (1)'
                    if line.split('$')[1] != 'krb5tgs':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, TGS-REP (AES256-CTS-HMAC-SHA1-96) (2)'
                    if line.split('$')[2] != '18':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, TGS-REP (AES256-CTS-HMAC-SHA1-96) (3)'
                elif hash_type == '19800':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 5 and dollar_cnt != 6:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, Pre-Auth (1)'
                    if line.split('$')[1] != 'krb5pa':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, Pre-Auth (2)'
                    if line.split('$')[2] != '17':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 17, Pre-Auth (3)'
                elif hash_type == '19900':
                    # This is slow af :(
                    for char in line:
                        if char == '$':
                            dollar_cnt += 1
                    if dollar_cnt != 5 and dollar_cnt != 6:
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, Pre-Auth (1)'
                    if line.split('$')[1] != 'krb5pa':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, Pre-Auth (2)'
                    if line.split('$')[2] != '18':
                        return 'Error line ' + str(line_number) + '. Doesnt appear to be of the type: Kerberos 5, etype 18, Pre-Auth (3)'
                else:
                    return 'Sorry. The only suppported Hash Types are: 7500, 13100, 18200, 19600, 19700, 19800 and 19900.'

            elif file_type == 'NetNTLM':
                # Excellent oppertunity to unique sort usernames and return error for duplicates
                if ':' not in line:
                    return 'Error line ' + str(line_number) + ' is missing a : character. NetNTLM file should include usernames.'
                # This is slow af :(
                colon_cnt = 0
                for char in line:
                    if char == ':':
                        colon_cnt += 1
                if colon_cnt < 5:
                    return 'Error line ' + str(line_number) + '. File does not appear to be be in a NetNTLM format.'

    return False

def getTimeFormat(total_runtime): # Runtime in seconds
    if total_runtime >= 604800:
        return str(round(total_runtime/604800)) + " week(s)"
    elif total_runtime >= 86400:
        return str(round(total_runtime/86400)) + " day(s)"
    elif total_runtime >= 3600:
        return str(round(total_runtime/3600)) + " hour(s)"
    elif total_runtime >= 60:
        return str(round(total_runtime/60)) + " minute(s)"
    elif total_runtime < 60:
        return "less then 1 minute"
