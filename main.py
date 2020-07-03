import os
import base64
import json
import atexit
import time
import sys
import re
from subprocess import Popen
from hashlib import md5
from smtplib import SMTP_SSL

from flask import Flask, request, send_from_directory, render_template
#from flask.ext.compress import Compress

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import pyotp

from utils import *
from creds import *

GENERATION_SCHEDULE_INTERVAL = 15
CONVERSION_SCHEDULE_INTERVAL = 15
API_LIMIT = 90
EPOCH_LENGTH = 110
MAX_ROOMS = 35
MAX_ROUNDS = 16
MAX_EMAIL_LENGTH = 254
CONVERSION_REPEAT_DELAY = 5
api_calls_in_epoch = 0
api_calls_from_last = 0
last_epoch_start = int(time.time())

otp_seed = os.urandom(12)
hotp = pyotp.HOTP(base64.b32encode(otp_seed).decode("utf-8"))
counter = 0

app = Flask("Scoresheet Generator")

queue = []
sqbs_queue = []


def schedule_generation():
    global queue
    global last_epoch_start
    global api_calls_from_last
    global api_calls_in_epoch
    if(int(time.time()) - last_epoch_start > EPOCH_LENGTH):
        api_calls_in_epoch = 0
        last_epoch_start = int(time.time())
    for i in range(len(queue)):
        (filename, num_api_calls) = queue[i]
        if(api_calls_in_epoch + num_api_calls < API_LIMIT):
            api_calls_in_epoch += num_api_calls
            api_calls_from_last = num_api_calls
            queue.pop(i)
            print("running:", filename, os.path.join(
                "generation_configs", filename))
            Popen([sys.executable, "scoresheetgen_with_rosters.py",
                   os.path.join("generation_configs", filename)])
            break


def schedule_sqbs_conversion():
    if(len(sqbs_queue) > 0):
        filename, rounds, rooms, powers, divisions = sqbs_queue.pop(0)
        print("converting:", filename)
        Popen([sys.executable, "convert_to_sqbs.py", filename, str(
            rooms), str(rounds), str(powers), str(divisions)])

# will modify in place


def validate_create_args(args):
    global counter
    err_dict = {
        "code": "Invalid access code",
        "sheet": "Invalid ID or url for master scoresheet",
        "agg": "Invalid ID or url for master aggregate sheet",
        "roster_id": "Invalid ID or url for roster sheet",
        "rooms": "Invalid number of rooms. Valid range: 1-{}".format(MAX_ROOMS),
        "num_rounds": "Invalid number of rounds. Valid range: 1-{}".format(MAX_ROUNDS),
        "email": "Invalid email",
        "duplicate_room_names": "You have a duplicate room name"
    }

    for key in ["sheet", "agg", "roster_id"]:
        if(key == "roster_id" and args["rosters"] != "true"):
            continue
        match = validate_spreadsheet(args[key])
        if(match):
            args[key] = match
        else:
            return {"error": err_dict[key]}

    if(args["rooms"].count(",") > 0):
        args["rooms"] = [i.strip()[:25]
                         for i in args["rooms"].split(",") if len(i.strip()) > 0]
    else:
        args["rooms"] = [i.strip()[:25]
                         for i in args["rooms"].split("\n") if len(i.strip()) > 0]
    if(len(set(args["rooms"])) != len(args["rooms"])):
        return {"error": err_dict["duplicate_room_names"]}

    if(len(args["rooms"]) > MAX_ROOMS or len(args["rooms"]) <= 0):
        return {"error": err_dict["rooms"]}

    email_match = re.match(
        r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', args["email"])
    if(not email_match or len(email_match.groups()[0]) > MAX_EMAIL_LENGTH):
        return {"error": err_dict["email"]}
    else:
        args["email"] = email_match.groups()[0]

    try:
        args["num_rounds"] = int(args["num_rounds"])
        assert args["num_rounds"] <= MAX_ROUNDS and args["num_rounds"] > 0
    except:
        return {"error": err_dict["num_rounds"]}

    if(args["code"] == hotp.at(counter)):
        counter += 1
        print("good", hotp.at(counter))
    else:
        return {"error": err_dict["code"]}
    return False


def validate_convert_args(args):
    global counter
    err_dict = {
        "agg": "Invalid ID or url for master aggregate sheet. Or you may not have chosen to use rosters",
        "email": "Invalid email",
        "num_rounds": "Invalid number of rounds",
        "num_rooms": "Invalid number of rooms"
    }
    email_match = re.match(
        r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', args["email"])
    if(not email_match or len(email_match.groups()[0]) > MAX_EMAIL_LENGTH):
        return {"error": err_dict["email"]}
    else:
        args["email"] = email_match.groups()[0]

    try:
        args["num_rounds"] = int(args["num_rounds"])
        assert args["num_rounds"] <= MAX_ROUNDS and args["num_rounds"] > 0
    except:
        return {"error": err_dict["num_rounds"]}
    try:
        args["num_rooms"] = int(args["num_rooms"])
        assert args["num_rooms"] <= MAX_ROOMS and args["num_rooms"] > 0
    except:
        return {"error": err_dict["num_rooms"]}

    match = validate_spreadsheet(args["agg"])
    if(match):
        if (os.path.isfile(os.path.join("sqbs_configs", generate_filename(match, ".json")))):
            args["agg"] = match
            return False

    return {"error": err_dict["agg"]}


@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


@app.route("/")
def serve_index():
    return render_template("info.html")


@app.route("/about")
def serve_about():
    return render_template("about.html")


@app.route("/create")
def create_index():
    # return serve_static("create_form.html")
    return render_template("create_form.html")


@app.route("/create/submit")
def create():
    global last_epoch_start
    req = dict((i, j.strip())
               for i, j in zip(request.args.keys(), request.args.values()))
    invalid = validate_create_args(req)
    if(invalid):
        return json.dumps(invalid)
    filename = generate_filename(req["sheet"], ".json")
    with open(os.path.join("generation_configs", filename), "w") as f:
        json.dump(req, f)
    if(len(queue) == 0):
        last_epoch_start = int(time.time())
    queue.append((filename, len(req["rooms"]) *
                  (2 if req["rosters"] == "true" else 0) + 1))
    return json.dumps({"success": req["email"]})


@app.route("/convert")
def convert_index():
    return render_template("convert_form.html")


@app.route("/convert/submit")
def convert():
    req = dict((i, j.strip())
               for i, j in zip(request.args.keys(), request.args.values()))
    invalid = validate_convert_args(req)
    if(invalid):
        return json.dumps(invalid)
    file = os.path.join("sqbs_configs", generate_filename(req["agg"], ".json"))
    d = {}
    with open(file) as f:
        d = json.load(f)
    if(int(time.time()) - d["last_run"] < CONVERSION_REPEAT_DELAY):
        return json.dumps({"error": "Please wait at least {} seconds in between submitting sqbs conversion jobs".format(CONVERSION_REPEAT_DELAY + CONVERSION_SCHEDULE_INTERVAL)})
    powers = int(req["powers"] == "true")
    divisions = int(req["divisions"] == "true")
    duplicate = False
    for item in queue:
        if(item[0] == file):
            duplicate = True
    if (duplicate):
        return json.dumps({"error": "You already have a job request for that aggregate sheet"})
    else:
        sqbs_queue.append(
            (file, req["num_rounds"], req["num_rooms"], powers, divisions))
        with open(file, "w") as f:
            d["email"] = req["email"]
            json.dump(d, f)
        return json.dumps({"success": "Your sqbs conversion job has been submitted. You should receive an email within the next few minutes with the resulting sqbs file"})


@app.route("/sqbs/<path:filename>")
def serve_sqbs_file(filename):
    return send_from_directory("sqbs_files", filename)


scheduler = BackgroundScheduler()
scheduler.start()
scheduler.add_job(func=schedule_generation, trigger=IntervalTrigger(
    seconds=GENERATION_SCHEDULE_INTERVAL), replace_existing=True)
scheduler.add_job(func=schedule_sqbs_conversion, trigger=IntervalTrigger(
    seconds=GENERATION_SCHEDULE_INTERVAL), replace_existing=True)
atexit.register(lambda: scheduler.shutdown())
if __name__ == '__main__':
    print(hotp.at(counter))
    # app.run("0.0.0.0", 8080)
    app.run()