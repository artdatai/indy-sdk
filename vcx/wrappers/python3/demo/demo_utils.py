import sys
import asyncio
import json
import random
from ctypes import cdll, CDLL
from time import sleep
import platform

import logging

from indy import wallet
from indy.error import ErrorCode, IndyError

from vcx.api.connection import Connection
from vcx.api.credential_def import CredentialDef
from vcx.api.issuer_credential import IssuerCredential
from vcx.api.credential import Credential
from vcx.api.proof import Proof
from vcx.api.disclosed_proof import DisclosedProof
from vcx.api.schema import Schema
from vcx.api.utils import vcx_agent_provision, vcx_messages_download
from vcx.api.vcx_init import vcx_init_with_config
from vcx.state import State, ProofState


async def create_schema_and_cred_def(schema_uuid, schema_name, schema_attrs, creddef_uuid, creddef_name):
    print("#3 Create a new schema on the ledger")
    version = format("%d.%d.%d" % (random.randint(1, 101), random.randint(1, 101), random.randint(1, 101)))
    schema = await Schema.create(schema_uuid, schema_name, version, schema_attrs, 0)
    schema_id = await schema.get_schema_id()

    print("#4 Create a new credential definition on the ledger")
    cred_def = await CredentialDef.create(creddef_uuid, creddef_name, schema_id, 0)
    cred_def_handle = cred_def.handle
    cred_def_id = await cred_def.get_cred_def_id()
    cred_def_json = await cred_def.serialize()
    print(" >>> cred_def_handle", cred_def_handle)

    return cred_def_json


async def send_credential_request(my_connection, cred_def_json, schema_attrs, cred_tag, cred_name):
    print("De-serialize cred def")
    cred_def = await CredentialDef.deserialize(cred_def_json)
    cred_def_handle = cred_def.handle
    print(" >>> cred_def_handle", cred_def_handle)

    print("#12 Create an IssuerCredential object using the schema and credential definition")
    credential = await IssuerCredential.create(cred_tag, schema_attrs, cred_def_handle, cred_name, '0')

    print("#13 Issue credential offer to X")
    await credential.send_offer(my_connection)

    # serialize/deserialize credential - waiting for Alice to rspond with Credential Request
    credential_data = await credential.serialize()

    while True:
        print("#14 Poll agency and wait for X to send a credential request")
        my_credential = await IssuerCredential.deserialize(credential_data)
        await my_credential.update_state()
        credential_state = await my_credential.get_state()
        if credential_state == State.RequestReceived:
            break
        else:
            credential_data = await my_credential.serialize()
            sleep(2)

    print("#17 Issue credential to X")
    await my_credential.send_credential(my_connection)

    # serialize/deserialize credential - waiting for Alice to accept credential
    credential_data = await my_credential.serialize()

    while True:
        print("#18 Wait for X to accept credential")
        my_credential2 = await IssuerCredential.deserialize(credential_data)
        await my_credential2.update_state()
        credential_state = await my_credential2.get_state()
        if credential_state == State.Accepted:
            break
        else:
            credential_data = await my_credential2.serialize()
            sleep(2)

    print("Done")


async def send_proof_request(my_connection, institution_did, proof_attrs, proof_uuid, proof_name):

    print("#19 Create a Proof object")
    proof = await Proof.create(proof_uuid, proof_name, proof_attrs, {})

    print("#20 Request proof of degree from alice")
    await proof.request_proof(my_connection)

    # serialize/deserialize proof
    proof_data = await proof.serialize()

    while True:
        print("#21 Poll agency and wait for X to provide proof")
        my_proof = await Proof.deserialize(proof_data)
        await my_proof.update_state()
        proof_state = await my_proof.get_state()
        if proof_state == State.Accepted:
            break
        else:
            proof_data = await my_proof.serialize()
            sleep(2)

    print("#27 Process the proof provided by X")
    await my_proof.get_proof(my_connection)

    print("#28 Check if proof is valid")
    if my_proof.proof_state == ProofState.Verified:
        print("proof is verified!!")
    else:
        print("could not verify proof :(")

    print("Done")


async def handle_messages(my_connection, handled_offers, handled_requests):
    print("Check for and handle offers")
    offers = await Credential.get_offers(my_connection)

    for offer in offers:
        handled = False
        for handled_offer in handled_offers:
            if offer[0]['msg_ref_id'] == handled_offer['msg_ref_id']:
                print(">>> got back offer that was already handled", offer[0]['msg_ref_id'])
                handled = True
                break
        if not handled:
            save_offer = offer[0].copy()
            print(" >>> handling offer", save_offer['msg_ref_id'])
            await handle_credential_offer(my_connection, offer)
            handled_offers.append(save_offer)

    print("Check for and handle proof requests")
    requests = await DisclosedProof.get_requests(my_connection)
    for request in requests:
        print("request", type(request), request)
        handled = False
        for handled_request in handled_requests:
            if request['msg_ref_id'] == handled_request['msg_ref_id']:
                print(">>> got back request that was already handled", request['msg_ref_id'])
                handled = True
                break
        if not handled:
            save_request = request.copy()
            print(" >>> handling proof", save_request['msg_ref_id'])
            await handle_proof_request(my_connection, request)
            handled_requests.append(save_request)


async def handle_credential_offer(my_connection, offer):
    print("Handling offer ...")

    print("Create a credential object from the credential offer")
    credential = await Credential.create('credential', offer)

    print("#15 After receiving credential offer, send credential request")
    await credential.send_request(my_connection, 0)

    # serialize/deserialize credential - wait for Faber to send credential
    credential_data = await credential.serialize()

    while True:
        print("#16 Poll agency and accept credential offer from X")
        my_credential = await Credential.deserialize(credential_data)
        await my_credential.update_state()
        credential_state = await my_credential.get_state()
        if credential_state == State.Accepted:
            break
        else:
            credential_data = await my_credential.serialize()
            sleep(2)

    print("Accepted")


async def handle_proof_request(my_connection, request):
    print("Handling proof request ...")

    print("#23 Create a Disclosed proof object from proof request")
    proof = await DisclosedProof.create('proof', request)

    print("#24 Query for credentials in the wallet that satisfy the proof request")
    credentials = await proof.get_creds()

    # TODO list credentials and let Alice select
    # Use the first available credentials to satisfy the proof request
    for attr in credentials['attrs']:
        credentials['attrs'][attr] = {
            'credential': credentials['attrs'][attr][0]
        }

    print("#25 Generate the proof")
    await proof.generate_proof(credentials, {})

    # TODO figure out why this always segfaults
    print("#26 Send the proof to X")
    await proof.send_proof(my_connection)

    # serialize/deserialize proof
    proof_data = await proof.serialize()

    while True:
        print("#27 Poll agency and wait for X to accept proof")
        my_proof = await DisclosedProof.deserialize(proof_data)
        await my_proof.update_state()
        proof_state = await my_proof.get_state()
        if proof_state == State.Accepted:
            break
        else:
            proof_data = await my_proof.serialize()
            sleep(2)

    print("proof_state", proof_state)

    print("Sent")


def file_ext():
    if platform.system() == 'Linux':
        return '.so'
    elif platform.system() == 'Darwin':
        return '.dylib'
    elif platform.system() == 'Windows':
        return '.dll'
    else:
        return '.so'


# load postgres dll and configure postgres wallet
def load_postgres_plugin(provisionConfig):
    print("Initializing postgres wallet")
    stg_lib = cdll.LoadLibrary("libindystrgpostgres" + file_ext())
    result = stg_lib.postgresstorage_init()
    if result != 0:
        print("Error unable to load postgres wallet storage", result)
        sys.exit(0)

    provisionConfig['wallet_type'] = 'postgres_storage'
    provisionConfig['storage_config'] = '{"url":"localhost:5432"}'
    provisionConfig['storage_credentials'] = '{"account":"postgres","password":"mysecretpassword","admin_account":"postgres","admin_password":"mysecretpassword"}'

    print("Success, loaded postgres wallet storage")


async def create_postgres_wallet(provisionConfig):
    print("Provision postgres wallet in advance")
    wallet_config = {
        'id': provisionConfig['wallet_name'],
        'storage_type': provisionConfig['wallet_type'],
        'storage_config': json.loads(provisionConfig['storage_config']),
    }
    wallet_creds = {
        'key': provisionConfig['wallet_key'],
        'storage_credentials': json.loads(provisionConfig['storage_credentials']),
    }
    try:
        await wallet.create_wallet(json.dumps(wallet_config), json.dumps(wallet_creds))
    except IndyError as ex:
        if ex.error_code == ErrorCode.PoolLedgerConfigAlreadyExistsError:
            pass
    print("Postgres wallet provisioned")
