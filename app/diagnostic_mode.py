"""Release marker retaining isolated diagnostic-build support.

No persisted option is changed. Production releases must set this to False.
"""
DIAGNOSTIC_ONLY = False
# Access1 connector enabled again on request for the test rig (reader and relay
# disconnected), so access events can be observed end to end.
OPENVINO_TEST_BUILD = False


def start_operational_services(processor, start_connector):
    processor.diagnostic_only = DIAGNOSTIC_ONLY
    processor.access1 = None
    if not DIAGNOSTIC_ONLY:
        start_connector()
        processor.start()
    return not DIAGNOSTIC_ONLY
