import copy
from datetime import datetime
import time
from bson import ObjectId, Timestamp
import pymongo.mongo_client
import singer
import pymongo
from pymongo.read_concern import ReadConcern
from typing import Set, Dict, Optional, Generator
from pymongo.collection import Collection
from pymongo.database import Database
from singer import utils
from pymongo import MongoClient

from dz_mongodb.sync_strategies import common

LOGGER = singer.get_logger('dz_mongodb')

RESUME_TOKEN_KEY = 'token'
DEFAULT_AWAIT_TIME_MS = 1000  # the server default https://docs.mongodb.com/manual/reference/method/db.watch/#db.watch
MIN_UPDATE_BUFFER_LENGTH = 1 # default
MAX_UPDATE_BUFFER_LENGTH = common.UPDATE_BOOKMARK_PERIOD # set max as the same value as bookmark period as we flush
# the buffer anyway after every UPDATE_BOOKMARK_PERIOD


def update_bookmarks(state: Dict, tap_stream_ids: Set[str], token: Dict) -> Dict:
    """
    Updates the stream state by re-setting the changeStream token
    Args:
        state: State dictionary
        tap_stream_ids: set of streams' ID
        token: resume token from changeStream

    Returns:
        state: updated state
    """
    for stream in tap_stream_ids:
        state = singer.write_bookmark(state, stream, RESUME_TOKEN_KEY, token)

    return state


def get_buffer_rows_from_db(collection: Collection,
                            update_buffer: Set,
                            ) -> Generator:
    """
    Fetches the full documents of the IDs in the buffer from the DB
    Args:
        collection: MongoDB Collection instance
        update_buffer: A set of IDs whose documents needs to be fetched
    Returns:
        generator: it can be empty
    """
    query = {'_id': {'$in': list(update_buffer)}}

    with collection.find(query) as cursor:
        yield from cursor


def get_token_from_state(streams_to_sync: Set[str], state: Dict) -> Optional[Dict]:
    """
    Extract the smallest non null resume token
    Args:
        streams_to_sync: set of log based streams
        state: state dictionary

    Returns: resume token if found, None otherwise

    """
    token_sorted = sorted([stream_state[RESUME_TOKEN_KEY]
                           for stream_name, stream_state in state.get('bookmarks', {}).items()
                           if stream_name in streams_to_sync and stream_state.get(RESUME_TOKEN_KEY) is not None],
                          key=lambda key: key['_data'])

    return token_sorted[0] if token_sorted else None

def check_resume_token_existance(client: MongoClient, resume_token_ts: datetime)->bool :
    """
    function check if provided timestamp is present in oplog or not
    """
    oplogRS = client["local"]["oplog.rs"]
    oplog_obj = oplogRS.find_one(sort = [("$natural", pymongo.ASCENDING)])
    first_oplog_ts =  oplog_obj.get("ts") if oplog_obj else None
    if not first_oplog_ts:
        raise Exception("unable to read first oplog for resume token verification")
    if resume_token_ts < first_oplog_ts.as_datetime():
        return False
    return True

def get_current_resume_token(database: Database, streams_to_sync: Dict[str, Dict]) -> Optional[str]:
    """
    returns current timestamp resume token or oldest resume token of active transactions
    """
    resume_token = None
    with database.watch(
            [{'$match': {
                '$or': [
                    {'operationType': 'insert'}, {'operationType': 'update'}, {'operationType': 'delete'}
                ],
                '$and': [
                    {'ns.coll': {'$in': [val['table_name'] for val in streams_to_sync.values()]}}
                ]
            }}]
    ) as temp_cursor:
        # Fetch the first change event to get the resume token
        resume_token = {
            '_data': temp_cursor.resume_token['_data']
        }
        temp_cursor.close()
    return resume_token

def sync_database(client: MongoClient,
                  db_name: str,
                  streams_to_sync: Dict[str, Dict],
                  state: Dict,
                  update_buffer_size: int,
                  await_time_ms: int,
                  full_load_on_empty_state: bool,
                  start_date: Optional[str],
                  document_remove: bool = False
                  ) -> None:
    """
    Syncs the records from the given collection using ChangeStreams
    Args:
        database: MongoDB Database instance to sync
        streams_to_sync: Dict of stream dictionary with all the stream details
        state: state dictionary
        update_buffer_size: the size of buffer used to hold detected updates
        await_time_ms:  the maximum time in milliseconds for the log based to wait for changes before exiting
    """
    database = client[db_name]
    LOGGER.info('Starting LogBased sync for streams "%s" in database "%s"', list(streams_to_sync.keys()), database.name)
    rows_saved = {}
    start_time = time.time()
    update_buffer = {}
    full_load = list()
    for stream_id in streams_to_sync:
        update_buffer[stream_id] = set()
        rows_saved[stream_id] = 0
        if full_load_on_empty_state:
            # get streams where no previous token exists in state
            if not singer.get_bookmark(state, stream_id, 'token'):
                full_load.append(stream_id)


    stream_ids = set(streams_to_sync.keys())
    start_after = get_token_from_state(stream_ids, state)
    # perform full load if no previous token exists
    if full_load_on_empty_state:
        # if start_after not present that means it is fist sync and with full load
        if not start_after:
            # preserve resume token from oplog
            start_after = get_current_resume_token(database, streams_to_sync)
        for tap_stream_id in full_load:
            table_name = streams_to_sync[tap_stream_id].get('table_name')
            collection = database[table_name]
            filter = {}
            if start_date:
                start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
                filter = { "_id": { "$gte": ObjectId.from_datetime(start_datetime) }}
                LOGGER.info('using filter for date[%s] to fetch data: %s',start_datetime, filter)

            # TODO: add batches
            # We can't use no timeout for some atlas tiers, also no_cursor_timeout only works for 30 mins.
            with collection.find(filter,sort=[("_id", pymongo.ASCENDING)]) as cursor:
                for row in cursor:
                    rows_saved[tap_stream_id] += 1
                    singer.write_message(common.row_to_singer_record(stream=streams_to_sync[tap_stream_id],
                                                                    row=row,
                                                                    time_extracted=utils.now(),
                                                                    time_deleted=None, document_remove=document_remove))

    LOGGER.info('starting change streams with resume token[%s]',start_after)
    # if there are no changes after MAX_AWAIT_TIME_MS, then we'll exit
    with database.watch(
            [{'$match': {
                '$or': [
                    {'operationType': 'insert'}, {'operationType': 'update'}, {'operationType': 'delete'}
                ],
                '$and': [
                    # watch collections of selected streams
                    {'ns.coll': {'$in': [val['table_name'] for val in streams_to_sync.values()]}}
                ]
            }}],
            max_await_time_ms=await_time_ms,
            start_after=start_after
    ) as cursor:
        while cursor.alive:

            change = cursor.try_next()

            # Note that the ChangeStream's resume token may be updated
            # even when no changes are returned.

            # Token can look like in MongoDB 4.2:
            #       {'_data': 'A_LONG_HEX_DECIMAL_STRING'}
            #    or {'_data': 'A_LONG_HEX_DECIMAL_STRING', '_typeBits': b'SOME_HEX'}

            # Get the '_data' only from resume token
            # token can contain a property '_typeBits' of type bytes which cannot be json
            # serialized when creating the state.
            # '_data' is enough to resume LOG_BASED
            resume_token = {
                '_data': cursor.resume_token['_data']
            }
            # After MAX_AWAIT_TIME_MS has elapsed, the cursor will return None.
            # write state and exit
            if change is None:
                LOGGER.info('No change streams after %s, updating bookmark and exiting...', await_time_ms)

                state = update_bookmarks(state, stream_ids, resume_token)
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

                break
            # can't use db name as sometime comes : 62a821b1fe15fd039ed2e450_test-users
            # or sometime test-users
            # database_name = change["ns"]["db"]
            # database_name = database_name.split('_', 1)[-1]
            if db_name not in change["ns"]["db"]:
                continue

            tap_stream_id = f'{db_name}-{change["ns"]["coll"]}'

            operation = change['operationType']

            if operation == 'insert':
                singer.write_message(common.row_to_singer_record(stream=streams_to_sync[tap_stream_id],
                                                                 row=change['fullDocument'],
                                                                 time_extracted=utils.now(),
                                                                 time_deleted=None, document_remove=document_remove))

                rows_saved[tap_stream_id] += 1

            elif operation == 'update':
                # update operation only return _id and updated fields in the row,
                # so saving _id for now until we fetch the document when it's time to flush the buffer
                update_buffer[tap_stream_id].add(change['documentKey']['_id'])

            elif operation == 'delete':
                # remove update from buffer if that document has been deleted
                update_buffer[tap_stream_id].discard(change['documentKey']['_id'])

                # Delete ops only contain the _id of the row deleted
                singer.write_message(common.row_to_singer_record(
                    stream=streams_to_sync[tap_stream_id],
                    row={'_id': change['documentKey']['_id']},
                    time_extracted=utils.now(),
                    time_deleted=change[
                        'clusterTime'].as_datetime(),document_remove=document_remove))  # returns python's datetime.datetime instance in UTC

                rows_saved[tap_stream_id] += 1
            # update the states of all streams
            state = update_bookmarks(state, stream_ids, resume_token)
            # flush buffer if it has filled up or flush and write state every UPDATE_BOOKMARK_PERIOD messages
            if sum(len(stream_buffer) for stream_buffer in update_buffer.values()) >= update_buffer_size or \
                    sum(rows_saved.values()) % common.UPDATE_BOOKMARK_PERIOD == 0:

                LOGGER.debug('Flushing update buffer ...')

                flush_buffer(update_buffer, streams_to_sync, database, rows_saved,document_remove)

                if sum(rows_saved.values()) % common.UPDATE_BOOKMARK_PERIOD == 0:
                    # write state
                    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    # flush buffer if finished with changeStreams
    flush_buffer(update_buffer, streams_to_sync, database, rows_saved,document_remove)

    for stream_id in stream_ids:
        common.COUNTS[stream_id] += rows_saved[stream_id]
        common.TIMES[stream_id] += time.time() - start_time
        LOGGER.info('Syncd %s records for %s', rows_saved[stream_id], stream_id)


def flush_buffer(buffer: Dict[str, Set], streams: Dict[str, Dict], database: Database, rows_saved: Dict[str, int],document_remove: bool = False):
    """
    Flush and reset the given buffer, it increments the row_saved count in the given rows_saved dictionary
    Args:
        database: mongoDB DB instance
        buffer: A set of rows to flush per stream
        streams: streams whose rows to flush
        rows_saved: map of streams to number of rows saved, this dictionary needs to be incremented
    Returns:

    """
    # flush all streams buffers
    for stream_id, stream_buffer in buffer.items():

        if stream_buffer:
            stream = streams[stream_id]

            for buffered_row in get_buffer_rows_from_db(database[stream['table_name']],
                                                        stream_buffer):
                record_message = common.row_to_singer_record(stream=stream,
                                                             row=buffered_row,
                                                             time_extracted=utils.now(),
                                                             time_deleted=None,document_remove=document_remove)
                singer.write_message(record_message)

                rows_saved[stream_id] += 1

            buffer[stream_id].clear()
