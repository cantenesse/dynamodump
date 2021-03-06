#!/usr/bin/env python
import json
import sys
import time
import shutil
import os
import argparse
import logging
import datetime
import threading
from boto.dynamodb2.layer1 import DynamoDBConnection
import boto.dynamodb2.layer1
from boto.s3.connection import S3Connection
from boto.s3.connection import Location
from boto.s3.connection import Key

JSON_INDENT = 2
AWS_SLEEP_INTERVAL = 10 #seconds
LOCAL_SLEEP_INTERVAL = 1 #seconds
MAX_BATCH_WRITE = 25 #DynamoDB limit
SCHEMA_FILE = "schema.json"
DATA_DIR = "data"
MAX_RETRY = 6
LOCAL_REGION = "local"
LOG_LEVEL = "INFO"
DUMP_PATH = "dump"
RESTORE_WRITE_CAPACITY = 100
THREAD_START_DELAY = 1 #seconds
CURRENT_WORKING_DIR = os.getcwd()
DEFAULT_PREFIX_SEPARATOR = "-"

def get_table_name_matches(conn, table_name_wildcard, separator):
  all_tables = []
  last_evaluated_table_name = None

  while True:
    table_list = conn.list_tables(exclusive_start_table_name=last_evaluated_table_name)
    all_tables.extend(table_list["TableNames"])

    try:
      last_evaluated_table_name = table_list["LastEvaluatedTableName"]
    except KeyError, e:
      break

  matching_tables = []
  for table_name in all_tables:
    if separator == None:
      if table_name.startswith(table_name_wildcard.split("*", 1)[0]):
        matching_tables.append(table_name)
    elif table_name.split(separator, 1)[0] == table_name_wildcard.split("*", 1)[0]:
      matching_tables.append(table_name)

  return matching_tables

def get_restore_table_matches(table_name_wildcard, separator):
  matching_tables = []
  try:
    dir_list = os.listdir("./" + DUMP_PATH)
  except OSError:
    logging.info("Cannot find \"./%s\", Now trying current working directory.." % DUMP_PATH)
    dump_data_path = CURRENT_WORKING_DIR
    try:
      dir_list = os.listdir(dump_data_path)
    except OSError:
      logging.info("Cannot find \"%s\" directory containing dump files!" % dump_data_path)
      sys.exit(1)

  for dir_name in dir_list:
    if dir_name.split(separator, 1)[0] == table_name_wildcard.split("*", 1)[0]:
      matching_tables.append(dir_name)

  return matching_tables

def change_prefix(source_table_name, source_wildcard, destination_wildcard, separator):
  source_prefix = source_wildcard.split("*", 1)[0]
  destination_prefix = destination_wildcard.split("*", 1)[0]
  if source_table_name.split(separator, 1)[0] == source_prefix:
    return destination_prefix + separator + source_table_name.split(separator, 1)[1]

def delete_table(conn, sleep_interval, table_name):
  while True:
    # delete table if exists
    table_exist = True
    try:
      conn.delete_table(table_name)
    except boto.exception.JSONResponseError, e:
      if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#ResourceNotFoundException":
        table_exist = False
        logging.info(table_name + " table deleted!")
        break
      elif e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
        logging.info("Limit exceeded, retrying deletion of " + table_name + "..")
        time.sleep(sleep_interval)
      elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
        logging.info("Control plane limit exceeded, retrying deletion of " + table_name + "..")
        time.sleep(sleep_interval)
      elif e.body["__type"] == "com.amazonaws.dynamodb.v20120810#ResourceInUseException":
        logging.info(table_name + " table is being deleted..")
        time.sleep(sleep_interval)
      else:
        logging.exception(e)
        sys.exit(1)

  # if table exists, wait till deleted
  if table_exist:
    try:
      while True:
        logging.info("Waiting for " + table_name + " table to be deleted.. [" + conn.describe_table(table_name)["Table"]["TableStatus"] +"]")
        time.sleep(sleep_interval)
    except boto.exception.JSONResponseError, e:
      if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#ResourceNotFoundException":
        logging.info(table_name + " table deleted.")
        pass
      else:
        logging.exception(e)
        sys.exit(1)

def mkdir_p(path):
  try:
    os.makedirs(path)
  except OSError as exc:
    if exc.errno == errno.EEXIST and os.path.isdir(path):
      pass
    else: raise

def batch_write(conn, sleep_interval, table_name, put_requests):
  request_items = {table_name: put_requests}
  i = 1
  while True:
    response = conn.batch_write_item(request_items)
    unprocessed_items = response["UnprocessedItems"]

    if len(unprocessed_items) == 0:
      break

    if len(unprocessed_items) > 0 and i <= MAX_RETRY:
      logging.debug(str(len(unprocessed_items)) + " unprocessed items, retrying.. [" + str(i) + "]")
      request_items = unprocessed_items
      i += 1
    else:
      logging.info("Max retries reached, failed to processed batch write: " + json.dumps(unprocessed_items, indent=JSON_INDENT))
      logging.info("Ignoring and continuing..")
      break

def wait_for_active_table(conn, table_name, verb):
  while True:
    if conn.describe_table(table_name)["Table"]["TableStatus"] != "ACTIVE":
      logging.info("Waiting for " + table_name + " table to be " + verb + ".. [" + conn.describe_table(table_name)["Table"]["TableStatus"] +"]")
      time.sleep(sleep_interval)
    else:
      logging.info(table_name + " " + verb + ".")
      break

def update_provisioned_throughput(conn, table_name, read_capacity, write_capacity, wait=True):
  logging.info("Updating " + table_name + " table read capacity to: " + str(read_capacity) + ", write capacity to: " + str(write_capacity))
  while True:
    try:
      conn.update_table(table_name, {"ReadCapacityUnits": int(read_capacity), "WriteCapacityUnits": int(write_capacity)})
      break
    except boto.exception.JSONResponseError, e:
      if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
        logging.info("Limit exceeded, retrying updating throughput of " + table_name + "..")
        time.sleep(sleep_interval)
      elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
        logging.info("Control plane limit exceeded, retrying updating throughput of " + table_name + "..")
        time.sleep(sleep_interval)

  # wait for provisioned throughput update completion
  if wait:
    wait_for_active_table(conn, table_name, "updated")

def connect_to_s3():
  conn = S3Connection()

  return conn

def create_s3_bucket(conn, region, bucket):
  try:
    bucket_id = conn.create_bucket(bucket)
    logging.info("Created bucket: %s" % (bucket))
  except boto.exception.S3CreateError:
    bucket_id = conn.get_bucket(bucket)
    logging.info("Bucket: %s already exists" % (bucket))

  return bucket_id

def s3_file_write(conn, bucket_id, content, path):
  k = Key(bucket_id)
  logging.info("Creating file: %s" % (path))
  k.key = path
  k.set_contents_from_string(content)

def s3_file_read(conn, bucket_id, path):
  content = bucket_id.get_contents_as_string(path)

  return content

def do_backup(conn, table_name, read_capacity,
              s3conn, s3bucket, s3location, dump_path):
  # if dump path is passed in, override the default dump path
  if dump_path:
    DUMP_PATH = dump_path

  logging.info("Starting backup for " + table_name + "..")

  # if s3 bucket is passed, create <bucket>/<table name>
  if s3bucket:
    bucket_id = create_s3_bucket(s3conn, s3location, s3bucket)
  else:
    # trash data, re-create subdir
    if os.path.exists(DUMP_PATH + "/" + table_name):
      shutil.rmtree(DUMP_PATH + "/" + table_name)
    mkdir_p(DUMP_PATH + "/" + table_name)

  table_desc = conn.describe_table(table_name)

  # if s3bucket is passed, create schema file in bucket
  if s3bucket:
    path = DUMP_PATH + "/" + table_name + "/" + SCHEMA_FILE
    content = json.dumps(table_desc, indent=JSON_INDENT)
    s3_file_write(s3conn, bucket_id, content, path)
  else:
    # get table schema
    logging.info("Dumping table schema for " + table_name)
    f = open(DUMP_PATH + "/" + table_name + "/" + SCHEMA_FILE, "w+")
    f.write(json.dumps(table_desc, indent=JSON_INDENT))
    f.close()

  original_read_capacity = table_desc["Table"]["ProvisionedThroughput"]["ReadCapacityUnits"]
  original_write_capacity = table_desc["Table"]["ProvisionedThroughput"]["WriteCapacityUnits"]

  # override table read capacity if specified
  if read_capacity != None and read_capacity != original_read_capacity:
    update_provisioned_throughput(conn, table_name, read_capacity, original_write_capacity)

  # get table data
  logging.info("Dumping table items for " + table_name)

  i = 1
  last_evaluated_key = None

  while True:
    scanned_table = conn.scan(table_name, exclusive_start_key=last_evaluated_key)

    if s3bucket:
      path = DUMP_PATH + "/" + table_name + "/" + DATA_DIR + "/" + str(i).zfill(4) + ".json"
      content = json.dumps(scanned_table, indent=JSON_INDENT)
      s3_file_write(s3conn, bucket_id, content, path)
    else:
      mkdir_p(DUMP_PATH + "/" + table_name + "/" + DATA_DIR)
      f = open(DUMP_PATH + "/" + table_name + "/" + DATA_DIR + "/" + str(i).zfill(4) + ".json", "w+")
      f.write(json.dumps(scanned_table, indent=JSON_INDENT))
      f.close()

    i += 1
    try:
      last_evaluated_key = scanned_table["LastEvaluatedKey"]
    except KeyError, e:
      break

  # revert back to original table read capacity if specified
  if read_capacity != None and read_capacity != original_read_capacity:
    update_provisioned_throughput(conn, table_name, original_read_capacity, original_write_capacity, False)

  logging.info("Backup for " + table_name + " table completed. Time taken: " + str(datetime.datetime.now().replace(microsecond=0) - start_time))

def do_restore(conn, sleep_interval, source_table,
               destination_table, write_capacity,
               s3conn, s3bucket, dump_path):
  if dump_path:
    DUMP_PATH = dump_path

  logging.info("Starting restore for " + source_table + " to " + destination_table + "..")

  if s3bucket:
    try:
      bucket = s3conn.get_bucket(s3bucket)
    except boto.exception.S3ResponseError:
      logging.info("Bucket: %s does not exist" % s3bucket)

    print dump_path
    for f in bucket.list(prefix=dump_path):
      print f.name

    sys.exit()

  # create table using schema
  # restore source_table from dump directory if it exists else try current working directory
  if os.path.exists("%s/%s" % (DUMP_PATH, source_table)):
    dump_data_path = DUMP_PATH
  else:
    logging.info("Cannot find \"./%s/%s\", Now trying current working directory.." % (DUMP_PATH, source_table))
    if os.path.exists("%s/%s" % (CURRENT_WORKING_DIR, source_table)):
      dump_data_path = CURRENT_WORKING_DIR
    else:
      logging.info("Cannot find \"%s/%s\" directory containing dump files!" % (CURRENT_WORKING_DIR, source_table))
      sys.exit(1)
  table_data = json.load(open(dump_data_path + "/" + source_table + "/" + SCHEMA_FILE))
  table = table_data["Table"]
  table_attribute_definitions = table["AttributeDefinitions"]
  table_table_name = destination_table
  table_key_schema = table["KeySchema"]
  original_read_capacity = table["ProvisionedThroughput"]["ReadCapacityUnits"]
  original_write_capacity = table["ProvisionedThroughput"]["WriteCapacityUnits"]
  table_local_secondary_indexes = table.get("LocalSecondaryIndexes")
  table_global_secondary_indexes = table.get("GlobalSecondaryIndexes")

  # override table write capacity if specified, else use RESTORE_WRITE_CAPACITY if original write capacity is lower
  if write_capacity == None:
    if original_write_capacity < RESTORE_WRITE_CAPACITY:
      write_capacity = RESTORE_WRITE_CAPACITY
    else:
      write_capacity = original_write_capacity

  # override GSI write capacities if specified, else use RESTORE_WRITE_CAPACITY if original write capacity is lower
  original_gsi_write_capacities = []
  if table_global_secondary_indexes is not None:
    for gsi in table_global_secondary_indexes:
      original_gsi_write_capacities.append(gsi["ProvisionedThroughput"]["WriteCapacityUnits"])

      if gsi["ProvisionedThroughput"]["WriteCapacityUnits"] < RESTORE_WRITE_CAPACITY:
        gsi["ProvisionedThroughput"]["WriteCapacityUnits"] = RESTORE_WRITE_CAPACITY

  # temp provisioned throughput for restore
  table_provisioned_throughput = {"ReadCapacityUnits": int(original_read_capacity), "WriteCapacityUnits": int(write_capacity)}

  logging.info("Creating " + destination_table + " table with temp write capacity of " + str(write_capacity))

  while True:
    try:
      conn.create_table(table_attribute_definitions, table_table_name, table_key_schema, table_provisioned_throughput, table_local_secondary_indexes, table_global_secondary_indexes)
      break
    except boto.exception.JSONResponseError, e:
      if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
        logging.info("Limit exceeded, retrying creation of " + destination_table + "..")
        time.sleep(sleep_interval)
      elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
        logging.info("Control plane limit exceeded, retrying creation of " + destination_table + "..")
        time.sleep(sleep_interval)
      else:
        logging.exception(e)
        sys.exit(1)

  # wait for table creation completion
  wait_for_active_table(conn, destination_table, "created")

  # read data files
  logging.info("Restoring data for " + destination_table + " table..")
  data_file_list = os.listdir(dump_data_path + "/" + source_table + "/" + DATA_DIR + "/")
  data_file_list.sort()

  for data_file in data_file_list:
    logging.info("Processing " + data_file + " of " + destination_table)
    items = []
    item_data = json.load(open(dump_data_path + "/" + source_table + "/" + DATA_DIR + "/" + data_file))
    items.extend(item_data["Items"])

    # batch write data
    put_requests = []
    while len(items) > 0:
      put_requests.append({"PutRequest": {"Item": items.pop(0)}})

      # flush every MAX_BATCH_WRITE
      if len(put_requests) == MAX_BATCH_WRITE:
        logging.debug("Writing next " + str(MAX_BATCH_WRITE) + " items to " + destination_table + "..")
        batch_write(conn, sleep_interval, destination_table, put_requests)
        del put_requests[:]

    # flush remainder
    if len(put_requests) > 0:
      batch_write(conn, sleep_interval, destination_table, put_requests)

  # revert to original table write capacity if it has been modified
  if write_capacity != original_write_capacity:
    update_provisioned_throughput(conn, destination_table, original_read_capacity, original_write_capacity, False)

  # loop through each GSI to check if it has changed and update if necessary
  if table_global_secondary_indexes is not None:
    gsi_data = []
    for gsi in table_global_secondary_indexes:
      original_gsi_write_capacity = original_gsi_write_capacities.pop(0)
      if original_gsi_write_capacity != gsi["ProvisionedThroughput"]["WriteCapacityUnits"]:
        gsi_data.append({"Update": { "IndexName" : gsi["IndexName"], "ProvisionedThroughput": { "ReadCapacityUnits": int(gsi["ProvisionedThroughput"]["ReadCapacityUnits"]), "WriteCapacityUnits": int(original_gsi_write_capacity),},},})

    logging.info("Updating " + destination_table + " global secondary indexes write capacities as necessary..")
    while True:
      try:
        conn.update_table(destination_table, global_secondary_index_updates=gsi_data)
        break
      except boto.exception.JSONResponseError, e:
        if e.body["__type"] == "com.amazonaws.dynamodb.v20120810#LimitExceededException":
          logging.info("Limit exceeded, retrying updating throughput of GlobalSecondaryIndexes in " + destination_table + "..")
          time.sleep(sleep_interval)
        elif e.body["__type"] == "com.amazon.coral.availability#ThrottlingException":
          logging.info("Control plane limit exceeded, retrying updating throughput of GlobalSecondaryIndexes in " + destination_table + "..")
          time.sleep(sleep_interval)

  logging.info("Restore for " + source_table + " to " + destination_table + " table completed. Time taken: " + str(datetime.datetime.now().replace(microsecond=0) - start_time))

if __name__ == '__main__':
# parse args
  parser = argparse.ArgumentParser(description="Simple DynamoDB backup/restore.")
  parser.add_argument("-m", "--mode",
    help="'backup' or 'restore'")
  parser.add_argument("-r", "--region",
    help="AWS region to use, e.g. 'us-west-1'. Use '" + LOCAL_REGION + "' for local DynamoDB testing.")
  parser.add_argument("-s", "--srcTable",
    help="Source DynamoDB table name to backup or restore from, use 'tablename*' for wildcard prefix selection")
  parser.add_argument("-d", "--destTable",
    help="Destination DynamoDB table name to backup or restore to, use 'tablename*' for wildcard prefix selection (defaults to use '-' separator) [optional, defaults to source]")
  parser.add_argument("--prefixSeparator",
    help="Specify a different prefix separator, e.g. '.' [optional]")
  parser.add_argument("--noSeparator",
    action='store_true',
    help="Overrides the use of a prefix separator for backup wildcard searches, [optional]")
  parser.add_argument("--readCapacity",
    help="Change the temp read capacity of the DynamoDB table to backup from [optional]")
  parser.add_argument("--writeCapacity",
    help="Change the temp write capacity of the DynamoDB table to restore to [defaults to " + str(RESTORE_WRITE_CAPACITY) + ", optional]")
  parser.add_argument("--host",
    help="Host of local DynamoDB [required only for local]")
  parser.add_argument("--port",
    help="Port of local DynamoDB [required only for local]")
  parser.add_argument("--accessKey",
    help="Access key of local DynamoDB [required only for local]")
  parser.add_argument("--secretKey",
    help="Secret key of local DynamoDB [required only for local]")
  parser.add_argument("--log",
    help="Logging level - DEBUG|INFO|WARNING|ERROR|CRITICAL [optional]")
  parser.add_argument("--s3bucket",
    help="Name of the s3 bucket to use for backup or restore")
  parser.add_argument("--s3location",
    help="Location where the s3 bucket resides")
  parser.add_argument("--dumpPath",
    help="Path where backup will be stored or restore will be taken from")
  args = parser.parse_args()

  # if s3bucket is specified, connect once and pass the connection around
  # during execution
  if args.s3bucket:
    s3_conn = connect_to_s3()

  # set log level
  log_level = LOG_LEVEL
  if args.log != None:
    log_level = args.log.upper()
  logging.basicConfig(level=getattr(logging, log_level))

  # instantiate connection
  if args.region == LOCAL_REGION:
    conn = DynamoDBConnection(
      aws_access_key_id=args.accessKey,
      aws_secret_access_key=args.secretKey,
      host=args.host,
      port=int(args.port),
      is_secure=False
    )
    sleep_interval = LOCAL_SLEEP_INTERVAL
  else:
    conn = boto.dynamodb2.connect_to_region(
      args.region,
      aws_access_key_id=args.accessKey,
      aws_secret_access_key=args.secretKey
    )
    sleep_interval = AWS_SLEEP_INTERVAL

  # set prefix separator
  prefix_separator = DEFAULT_PREFIX_SEPARATOR
  if args.prefixSeparator != None:
    prefix_separator = args.prefixSeparator
  if args.noSeparator == True:
    prefix_separator = None

  # do backup/restore
  start_time = datetime.datetime.now().replace(microsecond=0)
  if args.mode == "backup":
    if args.srcTable.find("*") != -1:
      matching_backup_tables = get_table_name_matches(conn, args.srcTable, prefix_separator)
      logging.info("Found " + str(len(matching_backup_tables)) + " table(s) in DynamoDB host to backup: " + ", ".join(matching_backup_tables))

      threads = []
      for table_name in matching_backup_tables:
        t = threading.Thread(target=do_backup,
          args=(conn, table_name,
                args.readCapacity, args.s3bucket,
                args.s3location, args.dumpPath))
        threads.append(t)
        t.start()
        time.sleep(THREAD_START_DELAY)

      for thread in threads:
        thread.join()

      logging.info("Backup of table(s) " + args.srcTable + " completed!")
    else:
      do_backup(conn, args.srcTable, args.readCapacity,
        s3_conn, args.s3bucket, args.s3location, args.dumpPath)
  elif args.mode == "restore":
    if args.destTable != None:
      dest_table = args.destTable
    else:
      dest_table = args.srcTable

    if dest_table.find("*") != -1:
      matching_destination_tables = get_table_name_matches(conn, dest_table, prefix_separator)
      logging.info("Found " + str(len(matching_destination_tables)) + " table(s) in DynamoDB host to be deleted: " + ", ".join(matching_destination_tables))

      threads = []
      for table_name in matching_destination_tables:
        t = threading.Thread(target=delete_table, args=(conn, sleep_interval, table_name,))
        threads.append(t)
        t.start()
        time.sleep(THREAD_START_DELAY)

      for thread in threads:
        thread.join()

      matching_restore_tables = get_restore_table_matches(args.srcTable, prefix_separator)
      logging.info("Found " + str(len(matching_restore_tables)) + " table(s) in " + DUMP_PATH + " to restore: " + ", ".join(matching_restore_tables))

      threads = []
      for source_table in matching_restore_tables:
        t = threading.Thread(target=do_restore,
                             args=(conn, sleep_interval,
                                   source_table, change_prefix(
                                    source_table,
                                    args.srcTable,
                                    dest_table,
                                    prefix_separator
                                  ),
                                   args.writeCapacity,
                                   s3_conn,
                                   args.s3bucket,
                                   args.dumpPath))
        threads.append(t)
        t.start()
        time.sleep(THREAD_START_DELAY)

      for thread in threads:
        thread.join()

      logging.info("Restore of table(s) " + args.srcTable + " to " +  dest_table + " completed!")
    else:
      delete_table(conn, sleep_interval, dest_table)
      do_restore(conn, sleep_interval, args.srcTable,
                 dest_table, args.writeCapacity, s3_conn,
                 args.s3bucket, args.dumpPath)

