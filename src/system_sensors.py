#!/usr/bin/env python3
import argparse
import datetime as dt
import signal
import sys
import socket
import platform
import threading
import time
from datetime import timedelta
from datetime import datetime
from re import findall
from subprocess import check_output
from rpi_bad_power import new_under_voltage
import paho.mqtt.client as mqtt
import psutil
import pytz
import yaml
import json
import csv
from pytz import timezone

try:
    import apt
    apt_disabled = False
except ImportError:
    apt_disabled = True
UTC = pytz.utc
DEFAULT_TIME_ZONE = None

DOCKER = {}
with open("/proc/1/cgroup", "rt") as dock:
    DOCKER = 'docker' in dock.read()

# Get OS information
OS_DATA = {}
with open("/etc/os-release") as f:
    reader = csv.reader(f, delimiter="=")
    for row in reader:
        if row:
            OS_DATA[row[0]] = row[1]

mqttClient = None
WAIT_TIME_SECONDS = 60
deviceName = None
_underVoltage = None

SENSOR_CONFIGS = {}
with open('sensor_configs.json') as json_file:
    SENSOR_CONFIGS = json.load(json_file)

class ProgramKilled(Exception):
    pass

def signal_handler(signum, frame):
    raise ProgramKilled

class Job(threading.Thread):
    def __init__(self, interval, execute, *args, **kwargs):
        threading.Thread.__init__(self)
        self.daemon = False
        self.stopped = threading.Event()
        self.interval = interval
        self.execute = execute
        self.args = args
        self.kwargs = kwargs

    def stop(self):
        self.stopped.set()
        self.join()

    def run(self):
        while not self.stopped.wait(self.interval.total_seconds()):
            self.execute(*self.args, **self.kwargs)


def write_log_message_to_console(message):
    print(datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ") + message)
    sys.stdout.flush()

def write_message_to_console(message):
    print(message)
    sys.stdout.flush()

def utc_from_timestamp(timestamp: float) -> dt.datetime:
    """Return a UTC time from a timestamp."""
    return UTC.localize(dt.datetime.utcfromtimestamp(timestamp))

def as_local(dattim: dt.datetime) -> dt.datetime:
    """Convert a UTC datetime object to local time zone."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        dattim = UTC.localize(dattim)

    return dattim.astimezone(DEFAULT_TIME_ZONE)

def get_last_boot():
    return str(as_local(utc_from_timestamp(psutil.boot_time())).isoformat())

def get_last_message():
    return str(as_local(utc_from_timestamp(time.time())).isoformat())

def on_message(client, userdata, message):
    print (f"Message received: {message.payload.decode()}"  )
    if(message.payload.decode() == "online"):
        send_config_message(client)


def updateSensors():
    payload_str = (
        '{'
        + f'"temperature": {get_temp()},'
        + f'"disk_use": {get_disk_usage("/")},'
        + f'"memory_use": {get_memory_usage()},'
        + f'"cpu_usage": {get_cpu_usage()},'
        + f'"swap_usage": {get_swap_usage()},'
        + f'"power_status": "{get_rpi_power_status()}",'
        + f'"last_boot": "{get_last_boot()}",'
        + f'"last_message": "{get_last_message()}",'
        + f'"host_name": "{get_host_name()}",'
        + f'"host_ip": "{get_host_ip()}",'
        + f'"host_os": "{get_host_os()}",'
        + f'"host_arch": "{get_host_arch()}"'
    )
    if "check_available_updates" in settings and settings["check_available_updates"] and not apt_disabled:
        payload_str = payload_str + f', "updates": {get_updates()}' 
    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        payload_str = payload_str + f', "wifi_strength": {get_wifi_strength()}'
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            payload_str = (
                payload_str + f', "disk_use_{drive.lower()}": {get_disk_usage(settings["external_drives"][drive])}'
            )
    payload_str = payload_str + "}"
    mqttClient.publish(
        topic=f"system-sensors/sensor/{deviceName}/state",
        payload=payload_str,
        qos=1,
        retain=False,
    )
    write_log_message_to_console("Payload published")


def get_updates():
    cache = apt.Cache()
    cache.open(None)
    cache.upgrade()
    return str(cache.get_changes().__len__())

def get_temp():
    temp = ""
    if "rasp" in OS_DATA["ID"]:
        reading = check_output(["vcgencmd", "measure_temp"]).decode("UTF-8")
        temp = str(findall("\d+\.\d+", reading)[0])
    else:
        reading = check_output(["cat", "/sys/class/thermal/thermal_zone0/temp"]).decode("UTF-8")
        temp = str(reading[0] + reading[1] + "." + reading[2])
    return temp

def get_disk_usage(path):
    return str(psutil.disk_usage(path).percent)


def get_memory_usage():
    return str(psutil.virtual_memory().percent)


def get_cpu_usage():
    return str(psutil.cpu_percent(interval=None))


def get_swap_usage():
    return str(psutil.swap_memory().percent)


def get_wifi_strength():  # check_output(["/proc/net/wireless", "grep wlan0"])
    wifi_strength_value = check_output(
                              [
                                  "bash",
                                  "-c",
                                  "cat /proc/net/wireless | grep wlan0: | awk '{print int($4)}'",
                              ]
                          ).decode("utf-8").rstrip()
    if not wifi_strength_value:
        wifi_strength_value = "0"
    return (wifi_strength_value)


def get_rpi_power_status():
    return _underVoltage.get()

def get_host_name():
    return socket.gethostname()

def get_host_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except socket.error:
        try:
            return socket.gethostbyname(socket.gethostname())
        except socket.gaierror:
            return '127.0.0.1'
    finally:
        sock.close()

def get_host_os():
    try:     
        return OS_DATA["PRETTY_NAME"]
    except:
        return "Unknown"

def get_host_arch():    
    try:     
        return platform.machine()
    except:
        return "Unknown"

def remove_old_topics():
    for sensor in SENSOR_CONFIGS["oldSensors"]:
        mqttClient.publish(
            topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}{sensor}/config",
            payload='',
            qos=1,
            retain=False,
        )
        write_log_message_to_console(f"Deleted legacy entry: {sensor}")

    for sensor in SENSOR_CONFIGS["oldBinary"]:
        mqttClient.publish(
            topic=f"homeassistant/binary_sensor/{deviceNameDisplay}/{deviceNameDisplay}{sensor}/config",
            payload='',
            qos=1,
            retain=False,
        )
        write_log_message_to_console(f"Deleted legacy entry: {sensor}")

    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            mqttClient.publish(
                topic=f"homeassistant/sensor/{deviceNameDisplay}/{deviceNameDisplay}DiskUse{drive}/config",
                payload='',
                qos=1,
                retain=False,
            )
        write_log_message_to_console(f"Deleted legacy entry: {drive}")

    write_log_message_to_console(" ")
    write_log_message_to_console(" ")


def check_settings(settings):
    if "mqtt" not in settings:
        write_log_message_to_console("Warning: MQTT not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "hostname" not in settings["mqtt"]:
        write_log_message_to_console("Warning: Hostname not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "timezone" not in settings:
        write_log_message_to_console("Warning: Timezone not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "deviceName" not in settings:
        write_log_message_to_console("Warning: DeviceName not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "client_id" not in settings:
        write_log_message_to_console("Warning: Client_id not defined in settings.yaml! Please check the documentation")
        sys.exit()
    if "power_integer_state" in settings:
        write_log_message_to_console("Warning: Power_integer_state is deprecated please remove this option power state is now a binary_sensor!")


payload_list = [['temperature', 'Temperature', 'sensor', '°C', 'mdi:thermometer'],
                ['disk_use', 'Disk Use', 'sensor', '%', 'mdi:micro-sd'],
                ['memory_use', 'Memory Use', 'sensor', '%', 'mdi:memory'],
                ['cpu_use', 'CPU Use', 'sensor', '%', 'mdi:memory'],
                ['disk_use', 'Disk Use', 'sensor', '%', 'mdi:harddisk'],
                ['swap_usage', 'Swap Use', 'sensor', '%', 'mdi:micro-sd'],
                ['power_status', 'Under Voltage', 'binary_sensor', '', 'mdi:power-plug'],
                ['last_boot', 'Last Boot', 'sensor', '', 'mdi:clock'],
                ['last_message', 'Last Message', 'sensor', '', 'mdi:clock-check'],
                ['hostname', 'Hostname', 'sensor', '', 'mdi:card-account-details'],
                ['host_ip', 'Host IP', 'sensor', '', 'mdi:lan'],
                ['host_os', 'Host OS', 'sensor', '', 'mdi:linux'],
                ['host_arch', 'Host Architecture', 'sensor', '', 'mdi:chip']]
                

def create_config_payload(sensorObject):
    try:
        if "unit" in sensorObject:
            mqttClient.publish(
                topic=f"homeassistant/{sensorObject['type']}/{deviceName}/{sensorObject['name_sys']}/config",
                payload="{"
                        + f"\"device_class\":\"{sensorObject['name_sys']}\","
                        + f"\"name\":\"{deviceNameDisplay} {sensorObject['name']}\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + f"\"unit_of_measurement\":\"{sensorObject['unit']}\","
                        + f"\"value_template\":\"{{{{value_json.{sensorObject['name_sys']}}}}}\","
                        + f"\"unique_id\":\"{deviceName}_sensor_{sensorObject['name_sys']}\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"RPI {deviceNameDisplay}\", \"manufacturer\":\"RPI\"}},"
                        + f"\"icon\":\"{sensorObject['icon']}\"}}",
                qos=1,
                retain=True,
            )
        else:
            mqttClient.publish(
                topic=f"homeassistant/{sensorObject['type']}/{deviceName}/{sensorObject['name_sys']}/config",
                payload="{"
                        + f"\"device_class\":\"{sensorObject['name_sys']}\","
                        + f"\"name\":\"{deviceNameDisplay} {sensorObject['name']}\","
                        + f"\"state_topic\":\"system-sensors/sensor/{deviceName}/state\","
                        + f"\"value_template\":\"{{{{value_json.{sensorObject['name_sys']}}}}}\","
                        + f"\"unique_id\":\"{deviceName}_sensor_{sensorObject['name_sys']}\","
                        + f"\"availability_topic\":\"system-sensors/sensor/{deviceName}/availability\","
                        + f"\"device\":{{\"identifiers\":[\"{deviceName}_sensor\"],"
                        + f"\"name\":\"{deviceNameDisplay} Sensors\",\"model\":\"RPI {deviceNameDisplay}\", \"manufacturer\":\"RPI\"}},"
                        + f"\"icon\":\"{sensorObject['icon']}\"}}",
                qos=1,
                retain=True,
            )

        write_log_message_to_console(f"Success sending \'{sensorObject['name']}\' config to broker")
    except:
        write_log_message_to_console(f"Error sending \'{sensorObject['name']}\' config to broker")


def send_config_message(mqttClient):

    write_log_message_to_console("Sending MQTT sensor config payloads to broker...")

    for sensor in SENSOR_CONFIGS["sensors"]:
        if 'toggle' not in sensor:
            create_config_payload(sensor)

#    for sensor_item in payload_list:
#        create_config_payload(sensor_item)

    if "check_available_updates" in settings and settings["check_available_updates"]:
        # import apt
        if(apt_disabled):
            write_log_message_to_console("Import of apt failed!")
        else:
            create_config_payload({'updates', 'Updates', 'sensor', '', 'mdi:cellphone-arrow-down'})
            

    if "check_wifi_strength" in settings and settings["check_wifi_strength"]:
        create_config_payload({'wifi_strength', 'WiFi Strength', 'sensor', 'dBm', 'mdi:wifi'})
        
    if "external_drives" in settings:
        for drive in settings["external_drives"]:
            create_config_payload({drive, 'Disk Use', 'sensor', '%', 'mdi:harddisk'})

    mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)


def _parser():
    """Generate argument parser"""
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", help="path to the settings file")
    return parser


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        write_log_message_to_console("Connected to broker")
        client.subscribe("hass/status")
        mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "online", retain=True)
    else:
        write_log_message_to_console("ERROR: Connection failed!")


write_message_to_console(" ")
write_message_to_console("------------------------------------------------------------------------")
write_message_to_console(" ")
write_message_to_console(".---..-..-..---..---..---..-.-.-. .---..---..-..-..---..----..---. .---.")
write_message_to_console(" \ \  >  /  \ \ `| |'| |- | | | |  \ \ | |- | .` | \ \ | || || |-<  \ \ ")
write_message_to_console("`---' `-'  `---' `-' `---'`-'-'-' `---'`---'`-'`-'`---'`----'`-'`-'`---'")
write_message_to_console(" ")
write_message_to_console("    System Sensors || https://github.com/Sennevds/system_sensors.git    ")
write_message_to_console(" ")
write_message_to_console("------------------------------------------------------------------------")
write_message_to_console(" ")
write_message_to_console(" ")


if __name__ == "__main__":
    args = _parser().parse_args()
    with open(args.settings) as f:
        # use safe_load instead load
        settings = yaml.safe_load(f)
    check_settings(settings)
    DEFAULT_TIME_ZONE = timezone(settings["timezone"])
    if "update_interval" in settings:
        WAIT_TIME_SECONDS = settings["update_interval"]
    mqttClient = mqtt.Client(client_id=settings["client_id"])
    mqttClient.on_connect = on_connect                      #attach function to callback
    mqttClient.on_message = on_message
    deviceName = settings["deviceName"].replace(" ", "").lower()
    deviceNameDisplay = settings["deviceName"]
    mqttClient.will_set(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
    if "user" in settings["mqtt"]:
        mqttClient.username_pw_set(
            settings["mqtt"]["user"], settings["mqtt"]["password"]
        )  # Username and pass if configured otherwise you should comment out this
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


    # INITIALISE MQTT BROKER CONNECTION
    try:
        if "port" in settings["mqtt"]:
            mqttClient.connect(settings["mqtt"]["hostname"], settings["mqtt"]["port"])
        else:
            mqttClient.connect(settings["mqtt"]["hostname"], 1883)
    except:
        write_log_message_to_console("Error! Could not connect to broker. Please check config...")
        exit() 


    # CLEAR UP OLD TOPICS AND SEND CONFIG PAYLOADS FOR EACH SENSOR
    try:
        remove_old_topics()
        send_config_message(mqttClient)
    except:
        write_log_message_to_console("Warning, something whent wrong when attempting to send config to broker")
    _underVoltage = new_under_voltage()

    
    # SETUP AND START MQTT PAYLOAD JOB
    job = Job(interval=timedelta(seconds=WAIT_TIME_SECONDS), execute=updateSensors)
    job.start()

    mqttClient.loop_start()

    while True:
        try:
            sys.stdout.flush()
            time.sleep(1)
        except ProgramKilled:
            write_message_to_console(" ")
            write_message_to_console(" ")
            write_log_message_to_console("Program killed! Running cleanup code")
            write_message_to_console(" ")
            mqttClient.publish(f"system-sensors/sensor/{deviceName}/availability", "offline", retain=True)
            mqttClient.disconnect()
            mqttClient.loop_stop()
            sys.stdout.flush()
            job.stop()
            write_message_to_console(" ")
            write_message_to_console(" ")
            break
