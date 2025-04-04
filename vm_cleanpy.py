import sys
import os
import re
import csv
import json
import base64
import subprocess
import logging
from boto3 import resource

# Setting up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
S3_BUCKET = os.getenv('S3_BUCKET', 'ib-onprem-cache-test')
VCENTER_CONFIGS = {
    "hq": {"url": os.getenv('HQ_VCENTER_URL', '10.39.23.100'), "cluster": os.getenv('HQ_CLUSTER_NAME', 'Automated-cleanup-servers')},
    "blr": {"url": os.getenv('BLR_VCENTER_URL', 'blr-devlab-vcenter.inblr.infoblox.com'), "cluster": os.getenv('BLR_CLUSTER_NAME', 'BLR-cleanup-servers')}
}
VCENTER_PASSWORD = base64.b64decode(os.getenv('VCENTER_PASSWORD_BASE64', 'SW5mb2Jsb3hAMTIzCg==')).decode('utf-8').replace('\n', '')

def pull_file_from_s3(**fileparams):
    """ Downloads a file from AWS S3 """
    try:
        obj = resource('s3').Bucket(fileparams["bucket"])
        for file_object in obj.objects.filter(Prefix=fileparams["file_name"]):
            obj.download_file(file_object.key, fileparams["dest_path"])
            logger.info("Successfully downloaded file from S3")
            return "successfully_downloaded"
    except Exception as err:
        logger.error(f"Error while downloading from S3: {err}")
        return "Failed_to_pull"

def push_file_to_s3(**fileparams):
    """ Uploads a file to AWS S3 """
    try:
        obj = resource('s3').Bucket(fileparams["bucket"])
        obj.upload_file(fileparams["source_file"], fileparams["file_name"])
        logger.info("Successfully uploaded file to S3")
    except Exception as err:
        logger.error(f"Error while uploading to S3: {err}")

def read_from_csv_file(file_path):
    """ Reads a CSV file and returns its contents """
    with open(file_path, newline='') as f:
        return list(csv.reader(f))

def write_to_csv_file(file_path, data):
    """ Writes data to a CSV file """
    with open(file_path, 'w', newline='') as f:
        csv.writer(f).writerows(data)

def fetch_vm_list_from_vcenter(vcenter_url, file_path):
    """ Fetches VM list from vCenter using govc and saves to a CSV file """
    try:
        command = ["govc", "find", ".", "-type", "VirtualMachine", "-json"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        vms = json.loads(result.stdout)["elements"]
        vm_data = [[vm.split('/')[-1], "0", ""] for vm in vms]  # Defaulting power-off time to 0
        write_to_csv_file(file_path, vm_data)
        logger.info(f"Fetched VM list from vCenter {vcenter_url} and saved to {file_path}")
    except Exception as e:
        logger.error(f"Failed to fetch VM list from vCenter: {e}")

def is_vm_excluded(vm_name, patterns):
    """ Checks if a VM matches any exclusion pattern """
    return any(re.search(pattern, vm_name, re.IGNORECASE) for pattern in patterns)

def is_powered_off_for_days(vm_entry, threshold_seconds):
    """ Checks if the VM has been powered off beyond the threshold """
    return int(vm_entry[1]) >= threshold_seconds

def has_email_tag(vm_tags):
    """ Checks if a VM has an email in its tags """
    return bool(re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', vm_tags))

def process_vcenter(vcenter_name, vcenter_url, cluster_name):
    """ Processes VMs for a given vCenter """
    logger.info(f"Processing cleanup for vCenter: {vcenter_name} ({vcenter_url})")

    # File configurations
    recent_vm_file = f"recent_vm_list_{vcenter_name}.csv"
    existing_vm_file = f"existing_vm_list_{vcenter_name}.csv"
    upload_vm_file = f"vcenter_{vcenter_name}_vm_list.csv"

    # Generate recent VM list if missing
    if not os.path.exists(recent_vm_file):
        fetch_vm_list_from_vcenter(vcenter_url, recent_vm_file)
    
    s3_file_params = {
        "bucket": S3_BUCKET,
        "file_name": f"env-1/{upload_vm_file}",
        "dest_path": existing_vm_file,
        "source_file": upload_vm_file,
    }

    if pull_file_from_s3(**s3_file_params) == "Failed_to_pull":
        logger.info("No history in S3. Uploading recent VM list.")
        push_file_to_s3(**s3_file_params)
        return

    # Exclusion lists
    mgmt_exclusion = ["vcentervm", "pxe server"]
    tinkaal_pattern = re.compile(r'^tinkaal|tinkaal$', re.IGNORECASE)
    powered_off_threshold = 30 * 24 * 60 * 60  # 30 days

    recent_vm_list = read_from_csv_file(recent_vm_file)
    existing_vm_list = read_from_csv_file(existing_vm_file)
    delete_list = []
    updated_recent_vm_list = []

    # Process VMs
    for entry in recent_vm_list:
        vm_name = entry[0]
        vm_tags = entry[2] if len(entry) > 2 else ""
        
        # Skip Tinkaal VMs
        if tinkaal_pattern.search(vm_name):
            logger.info(f"Skipping {vm_name} - Tinkaal VMs are not deleted.")
            updated_recent_vm_list.append(entry)
            continue

        # Management exclusions
        if is_vm_excluded(vm_name, mgmt_exclusion):
            logger.info(f"Excluded Management VM: {vm_name}")
            updated_recent_vm_list.append(entry)
            continue
        
        # Check if VM has an email tag
        if has_email_tag(vm_tags):
            logger.info(f"Email tag found for {vm_name}, skipping cleanup.")
            updated_recent_vm_list.append(entry)
            continue

        # Check powered-off status
        for old_entry in existing_vm_list:
            if old_entry[0] == vm_name and is_powered_off_for_days(old_entry, powered_off_threshold):
                logger.info(f"Power-off threshold exceeded for {vm_name}, marking for cleanup.")
                delete_list.append(entry)
                break
        else:
            updated_recent_vm_list.append(entry)

    write_to_csv_file(upload_vm_file, updated_recent_vm_list)
    push_file_to_s3(**s3_file_params)

    delete_vms = [entry[0] for entry in delete_list]
    delete_dict = {
        "hostname": vcenter_url,
        "username": "administrator@vsphere.local",
        "password": VCENTER_PASSWORD,
        "validate_certs": "no",
        "cluster": cluster_name,
        "vm_list": delete_vms,
    }

    with open(f'delete_vms_{vcenter_name}.json', 'w', encoding='utf-8') as f:
        json.dump(delete_dict, f, ensure_ascii=False, indent=4)
    
    logger.info(f"Cleanup process completed for {vcenter_name}.")

def main():
    # Run for both HQ and BLR vCenters
    for name, config in VCENTER_CONFIGS.items():
        process_vcenter(name, config["url"], config["cluster"])

if __name__ == "__main__":
    main()
