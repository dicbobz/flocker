# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Functional tests for ``flocker.node.agents.ebs`` using an EC2 cluster.

"""

from uuid import uuid4

from bitmath import Byte

from boto.ec2.volume import (
    Volume as EbsVolume, AttachmentSet
)
from boto.exception import EC2ResponseError

from twisted.python.constants import Names, NamedConstant
from twisted.trial.unittest import SkipTest, TestCase
from eliot.testing import LoggedMessage, capture_logging

from ..ebs import (
    _wait_for_volume_state_change, BOTO_EC2RESPONSE_ERROR,
    VolumeOperations, VolumeStateTable, VolumeStates,
    TimeoutException
)

from .._logging import (
    AWS_CODE, AWS_MESSAGE, AWS_REQUEST_ID, BOTO_LOG_HEADER,
)
from ..test.test_blockdevice import make_iblockdeviceapi_tests

from ..test.blockdevicefactory import (
    InvalidConfig, ProviderType, get_blockdeviceapi_args,
    get_blockdeviceapi_with_cleanup, get_device_allocation_unit,
    get_minimum_allocatable_size,
)

TIMEOUT = 5


def ebsblockdeviceapi_for_test(test_case):
    """
    Create an ``EBSBlockDeviceAPI`` for use by tests.
    """
    return get_blockdeviceapi_with_cleanup(test_case, ProviderType.aws)


class EBSBlockDeviceAPIInterfaceTests(
        make_iblockdeviceapi_tests(
            blockdevice_api_factory=(
                lambda test_case: ebsblockdeviceapi_for_test(
                    test_case=test_case,
                )
            ),
            minimum_allocatable_size=get_minimum_allocatable_size(),
            device_allocation_unit=get_device_allocation_unit(),
            unknown_blockdevice_id_factory=lambda test: u"vol-00000000",
        )
):

    """
    Interface adherence Tests for ``EBSBlockDeviceAPI``.
    """
    def test_foreign_volume(self):
        """
        Test that ``list_volumes`` lists only those volumes
        belonging to the current Flocker cluster.
        """
        try:
            cls, kwargs = get_blockdeviceapi_args(ProviderType.aws)
        except InvalidConfig as e:
            raise SkipTest(str(e))
        ec2_client = kwargs["ec2_client"]
        requested_volume = ec2_client.connection.create_volume(
            int(Byte(self.minimum_allocatable_size).to_GiB().value),
            ec2_client.zone)
        self.addCleanup(ec2_client.connection.delete_volume,
                        requested_volume.id)

        _wait_for_volume_state_change(VolumeOperations.CREATE,
                                      requested_volume)

        self.assertEqual(self.api.list_volumes(), [])

    def test_foreign_cluster_volume(self):
        """
        Test that list_volumes() excludes volumes belonging to
        other Flocker clusters.
        """
        blockdevice_api2 = ebsblockdeviceapi_for_test(
            test_case=self,
        )
        flocker_volume = blockdevice_api2.create_volume(
            dataset_id=uuid4(),
            size=self.minimum_allocatable_size,
        )
        self.assert_foreign_volume(flocker_volume)

    @capture_logging(lambda self, logger: None)
    def test_boto_ec2response_error(self, logger):
        """
        1. Test that invalid parameters to Boto's EBS API calls
        raise the right exception after logging to Eliot.
        2. Verify Eliot log output for expected message fields
        from logging decorator for boto.exception.EC2Exception
        originating from boto.ec2.connection.EC2Connection.
        """
        # Test 1: Create volume with size 0.
        # Raises: EC2ResponseError
        self.assertRaises(EC2ResponseError, self.api.create_volume,
                          dataset_id=uuid4(), size=0,)

        # Test 2: Set EC2 connection zone to an invalid string.
        # Raises: EC2ResponseError
        self.api.zone = u'invalid_zone'
        self.assertRaises(
            EC2ResponseError,
            self.api.create_volume,
            dataset_id=uuid4(),
            size=self.minimum_allocatable_size,
        )

        # Validate decorated method for exception logging
        # actually logged to ``Eliot`` logger.
        expected_message_keys = {AWS_CODE.key, AWS_MESSAGE.key,
                                 AWS_REQUEST_ID.key}
        for logged in LoggedMessage.of_type(logger.messages,
                                            BOTO_EC2RESPONSE_ERROR,):
            key_subset = set(key for key in expected_message_keys
                             if key in logged.message.keys())
            self.assertEqual(expected_message_keys, key_subset)

    @capture_logging(None)
    def test_boto_request_logging(self, logger):
        """
        Boto is configured to send log events to Eliot when it makes an AWS API
        request.
        """
        self.api.list_volumes()

        messages = list(
            message
            for message
            in logger.messages
            if message.get("message_type") == BOTO_LOG_HEADER
        )
        self.assertNotEqual(
            [], messages,
            "Didn't find Boto messages in logged messages {}".format(
                messages
            )
        )

    def test_next_device_in_use(self):
        """
        ``_next_device`` skips devices indicated as being in use.

        Ideally we'd have a test for this using the public API, but this
        only occurs if we hit eventually consistent ignorance in the AWS
        servers so it's hard to trigger deterministically.
        """
        result = self.api._next_device(self.api.compute_instance_id(), [],
                                       {u"/dev/sdf"})
        self.assertEqual(result, u"/dev/sdg")


class VolumeStateTransitionTests(TestCase):
    """
    Tests for volume state operations and resulting volume state changes.
    """

    class VolumeEndStateTypes(Names):
        """
        Types of volume states to simulate.
        """
        ERROR = NamedConstant()
        IN_TRANSIT = NamedConstant()
        DESTINATION = NamedConstant()

    class VolumeAttachDataTypes(Names):
        """
        Types of volume's attach data states to simulate.
        """
        MISSING = NamedConstant()
        MISSING_INSTANCE_ID = NamedConstant()
        MISSING_DEVICE = NamedConstant()
        ATTACH_SUCCESS = NamedConstant()
        DETACH_SUCCESS = NamedConstant()

    V = VolumeOperations
    S = VolumeEndStateTypes
    A = VolumeAttachDataTypes

    def _create_template_ebs_volume(self, operation):
        """
        Helper function to create template EBS volume to work on.

        :param NamedConstant operation: Intended use of created template.
            A value from ``VolumeOperations``.

        :returns: Suitable volume in the right start state for input operation.
        :rtype: boto.ec2.volume.Volume
        """
        volume = EbsVolume()

        # Irrelevant volume attributes.
        volume.id = u'vol-9c48a689'
        volume.create_time = u'2015-07-14T22:46:00.447Z'
        volume.size = 1
        volume.snapshot_id = ''
        volume.zone = u'us-west-2b'
        volume.type = u'standard'

        volume_state_table = VolumeStateTable()
        state_flow = volume_state_table.table[operation]
        start_state = state_flow.start_state.value

        # Interesting volume attribute.
        volume.status = start_state

        return volume

    def _pick_end_state(self, operation, state_type):
        """
        Helper function to pick a desired volume state for given input
        operation.

        :param NamedConstant operation: Volume operation to pick a
            state for. A value from ``VolumeOperations``.
        :param NamedConstant state_type: Volume state type request.

        :returns: A state from ``VolumeStates`` that will not be part of
            a volume's states resulting from input operation.
        :rtype: ValueConstant
        """
        volume_state_table = VolumeStateTable()
        state_flow = volume_state_table.table[operation]

        if state_type == self.S.ERROR:
            valid_states = set([state_flow.start_state,
                                state_flow.transient_state,
                                state_flow.end_state])

            err_states = set(VolumeStates._enumerants.values()) - valid_states
            err_state = err_states.pop()
            return err_state.value
        elif state_type == self.S.IN_TRANSIT:
            return state_flow.transient_state.value
        elif state_type == self.S.DESTINATION:
            return state_flow.end_state.value

    def _pick_attach_data(self, attach_type):
        """
        Helper function to create desired volume attach data.

        :param NamedConstant attach_type: Type of attach data to create.

        :returns: Volume attachment set that conforms to requested attach type.
        :rtype: AttachmentSet
        """
        if attach_type == self.A.MISSING:
            return None
        elif attach_type == self.A.MISSING_INSTANCE_ID:
            attach_data = AttachmentSet()
            attach_data.device = u'/dev/sdf'
            attach_data.instance_id = ''
            return attach_data
        elif attach_type == self.A.MISSING_DEVICE:
            attach_data = AttachmentSet()
            attach_data.device = ''
            attach_data.instance_id = u'i-xyz'
            return attach_data
        elif attach_type == self.A.ATTACH_SUCCESS:
            attach_data = AttachmentSet()
            attach_data.device = u'/dev/sdf'
            attach_data.instance_id = u'i-xyz'
            return attach_data
        elif attach_type == self.A.DETACH_SUCCESS:
            return None

    def _custom_update(self, operation, state_type, attach_data=A.MISSING):
        """
        Create a custom update function for a volume.
        """
        def update(volume):
            """
            Transition volume to desired end state and attach data.

            :param boto.ec2.volume.Volume volume: Volume to move to
                invalid state.
            """
            volume.status = self._pick_end_state(operation, state_type)
            volume.attach_data = self._pick_attach_data(attach_data)
        return update

    def _assert_raises_invalid_state(self, operation, testcase,
                                     attach_data_type=A.MISSING):
        """
        Helper function to validate that ``TimeoutException`` is raised as
        a result of performing input operation for given testcase on a volume.
        """
        volume = self._create_template_ebs_volume(operation)
        self.assertRaises(TimeoutException, _wait_for_volume_state_change,
                          operation, volume,
                          self._custom_update(operation, testcase,
                                              attach_data_type),
                          TIMEOUT)

    def _assert_success(self, operation, testcase,
                        attach_data_type=A.ATTACH_SUCCESS):
        """
        Helper function to validate that performing given operation for given
        testcase on a volume succeeds.
        """
        volume = self._create_template_ebs_volume(operation)
        _wait_for_volume_state_change(operation, volume,
                                      self._custom_update(operation, testcase,
                                                          attach_data_type),
                                      TIMEOUT)

        if operation == self.V.CREATE:
            self.assertEquals(volume.status, u'available')
        elif operation == self.V.DESTROY:
            self.assertEquals(volume.status, u'')
        elif operation == self.V.ATTACH:
            self.assertEqual([volume.status, volume.attach_data.device,
                              volume.attach_data.instance_id],
                             [u'in-use', u'/dev/sdf', u'i-xyz'])
        elif operation == self.V.DETACH:
            self.assertEqual(volume.status, u'available')

    def test_create_invalid_state(self):
        """
        Assert that ``TimeoutException`` is thrown if create fails.
        """
        self._assert_raises_invalid_state(self.V.CREATE, self.S.ERROR)

    def test_destroy_invalid_state(self):
        """
        Assert that ``TimeoutException`` is thrown if destroy fails.
        """
        self._assert_raises_invalid_state(self.V.DESTROY, self.S.ERROR)

    def test_attach_invalid_state(self):
        """
        Assert that ``TimeoutException`` is thrown if attach fails.
        """
        self._assert_raises_invalid_state(self.V.ATTACH, self.S.ERROR)

    def test_detach_invalid_state(self):
        """
        Assert that ``TimeoutException`` is thrown if detach fails.
        """
        self._assert_raises_invalid_state(self.V.DETACH, self.S.ERROR)

    def test_stuck_create(self):
        """
        Assert that ``TimeoutException`` is thrown if create gets stuck.
        """
        self._assert_raises_invalid_state(self.V.CREATE, self.S.IN_TRANSIT)

    def test_stuck_destroy(self):
        """
        Assert that ``TimeoutException`` is thrown if destroy gets stuck.
        """
        self._assert_raises_invalid_state(self.V.DESTROY, self.S.IN_TRANSIT)

    def test_stuck_attach(self):
        """
        Assert that ``TimeoutException`` is thrown if attach gets stuck.
        """
        self._assert_raises_invalid_state(self.V.ATTACH, self.S.IN_TRANSIT)

    def test_stuck_detach(self):
        """
        Assert that ``TimeoutException`` is thrown if detach gets stuck.
        """
        self._assert_raises_invalid_state(self.V.DETACH, self.S.IN_TRANSIT)

    def test_attach_missing_attach_data(self):
        """
        Assert that ``TimeoutException`` is thrown if attach fails to update
        AttachmentSet.
        """
        self._assert_raises_invalid_state(self.V.ATTACH, self.S.DESTINATION)

    def test_attach_missing_instance_id(self):
        """
        Assert that ``TimeoutException`` is thrown if attach fails to update
        volume's attach data for compute instance id.
        """
        self._assert_raises_invalid_state(self.V.ATTACH,
                                          self.S.DESTINATION,
                                          self.A.MISSING_INSTANCE_ID)

    def test_attach_missing_device(self):
        """
        Assert that ``TimeoutException`` is thrown if attach fails to update
        volume's attached device information.

        """
        self._assert_raises_invalid_state(self.V.ATTACH,
                                          self.S.DESTINATION,
                                          self.A.MISSING_DEVICE)

    def test_create_success(self):
        """
        Assert that successful volume creation leads to valid volume end state.
        """
        self._assert_success(self.V.CREATE, self.S.DESTINATION)

    def test_destroy_success(self):
        """
        Assert that successful volume destruction leads to valid end state.
        """
        self._assert_success(self.V.DESTROY, self.S.DESTINATION)

    def test_attach_sucess(self):
        """
        Test if successful attach volume operation leads to expected state.
        """
        self._assert_success(self.V.ATTACH, self.S.DESTINATION)

    def test_detach_success(self):
        """
        Test if successful detach volume operation leads to expected state.
        """
        self._assert_success(self.V.DETACH, self.S.DESTINATION,
                             self.A.DETACH_SUCCESS)
