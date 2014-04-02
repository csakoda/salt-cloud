'''
The EC2 Cloud Module
====================

The EC2 cloud module is used to interact with the Amazon Elastic Cloud
Computing. This driver is highly experimental! Use at your own risk!

To use the EC2 cloud module, when using the old format the following
configuration parameters need to be set in the main cloud configuration:

.. code-block:: yaml

    # The EC2 API authentication id
    EC2.id: GKTADJGHEIQSXMKKRBJ08H
    # The EC2 API authentication key
    EC2.key: askdjghsdfjkghWupUjasdflkdfklgjsdfjajkghs
    # The ssh keyname to use
    EC2.keyname: default
    # The amazon security group
    EC2.securitygroup: ssh_open
    # The location of the private key which corresponds to the keyname
    EC2.private_key: /root/default.pem

    # Be default, service_url is set to amazonaws.com. If you are using this
    # driver for something other than Amazon EC2, change it here:
    EC2.service_url: amazonaws.com

    # The endpoint that is ultimately used is usually formed using the region
    # and the service_url. If you would like to override that entirely, you can
    # explicitly define the endpoint:
    EC2.endpoint: myendpoint.example.com:1138/services/Cloud


Using the new format, set up the cloud configuration at
 ``/etc/salt/cloud.providers`` or ``/etc/salt/cloud.providers.d/ec2.conf``:

.. code-block:: yaml

    my-ec2-config:
      # The EC2 API authentication id
      id: GKTADJGHEIQSXMKKRBJ08H
      # The EC2 API authentication key
      key: askdjghsdfjkghWupUjasdflkdfklgjsdfjajkghs
      # The ssh keyname to use
      keyname: default
      # The amazon security group
      securitygroup: ssh_open
      # The location of the private key which corresponds to the keyname
      private_key: /root/default.pem

      # Be default, service_url is set to amazonaws.com. If you are using this
      # driver for something other than Amazon EC2, change it here:
      service_url: amazonaws.com

      # The endpoint that is ultimately used is usually formed using the region
      # and the service_url. If you would like to override that entirely, you
      # can explicitly define the endpoint:
      endpoint: myendpoint.example.com:1138/services/Cloud

      provider: ec2

'''

# Import python libs
import os
import sys
import stat
import time
import uuid
import pprint
import logging
import yaml
from time import sleep

# Import libs for talking to the EC2 API
import hmac
import hashlib
import binascii
import base64
import datetime
import urllib
import urllib2
import xml.etree.ElementTree as ET

# Import saltcloud libs
import saltcloud.utils
import saltcloud.config as config
from saltcloud.libcloudfuncs import *   # pylint: disable-msg=W0614,W0401
from saltcloud.exceptions import (
    SaltCloudException,
    SaltCloudSystemExit,
    SaltCloudConfigError,
    SaltCloudExecutionTimeout,
    SaltCloudExecutionFailure
)

# Get logging started
log = logging.getLogger(__name__)

SIZE_MAP = {
    'Micro Instance': 't1.micro',
    'Small Instance': 'm1.small',
    'Medium Instance': 'm1.medium',
    'Large Instance': 'm1.large',
    'Extra Large Instance': 'm1.xlarge',
    'High-CPU Medium Instance': 'c1.medium',
    'High-CPU Extra Large Instance': 'c1.xlarge',
    'High-Memory Extra Large Instance': 'm2.xlarge',
    'High-Memory Double Extra Large Instance': 'm2.2xlarge',
    'High-Memory Quadruple Extra Large Instance': 'm2.4xlarge',
    'Cluster GPU Quadruple Extra Large Instance': 'cg1.4xlarge',
    'Cluster Compute Quadruple Extra Large Instance': 'cc1.4xlarge',
    'Cluster Compute Eight Extra Large Instance': 'cc2.8xlarge',
}


EC2_LOCATIONS = {
    'ap-northeast-1': Provider.EC2_AP_NORTHEAST,
    'ap-southeast-1': Provider.EC2_AP_SOUTHEAST,
    'eu-west-1': Provider.EC2_EU_WEST,
    'sa-east-1': Provider.EC2_SA_EAST,
    'us-east-1': Provider.EC2_US_EAST,
    'us-west-1': Provider.EC2_US_WEST,
    'us-west-2': Provider.EC2_US_WEST_OREGON
}
DEFAULT_LOCATION = 'us-east-1'

if hasattr(Provider, 'EC2_AP_SOUTHEAST2'):
    EC2_LOCATIONS['ap-southeast-2'] = Provider.EC2_AP_SOUTHEAST2


# Only load in this module if the EC2 configurations are in place
def __virtual__():
    '''
    Set up the libcloud functions and check for EC2 configurations
    '''
    if get_configured_provider() is False:
        log.debug(
            'There is no EC2 cloud provider configuration available. Not '
            'loading module'
        )
        return False

    for provider, details in __opts__['providers'].iteritems():
        if 'provider' not in details or details['provider'] != 'ec2':
            continue

        if not os.path.exists(details['private_key']):
            raise SaltCloudException(
                'The EC2 key file {0!r} used in the {1!r} provider '
                'configuration does not exist\n'.format(
                    details['private_key'],
                    provider
                )
            )

        keymode = str(
            oct(stat.S_IMODE(os.stat(details['private_key']).st_mode))
        )
        if keymode not in ('0400', '0600'):
            raise SaltCloudException(
                'The EC2 key file {0!r} used in the {1!r} provider '
                'configuration needs to be set to mode 0400 or 0600\n'.format(
                    details['private_key'],
                    provider
                )
            )

    log.debug('Loading EC2 cloud compute module')
    return 'ec2'


def get_configured_provider():
    '''
    Return the first configured instance.
    '''
    return config.is_provider_configured(
        __opts__,
        __active_provider_name__ or 'ec2',
        ('id', 'key', 'keyname', 'private_key')
    )


def _xml_to_dict(xmltree):
    '''
    Convert an XML tree into a dict
    '''
    if sys.version_info < (2, 7):
        children_len = len(xmltree.getchildren())
    else:
        children_len = len(xmltree)

    if children_len < 1:
        name = xmltree.tag
        if '}' in name:
            comps = name.split('}')
            name = comps[1]
        return {name: xmltree.text}

    xmldict = {}
    for item in xmltree:
        name = item.tag
        if '}' in name:
            comps = name.split('}')
            name = comps[1]
        if not name in xmldict.keys():
            if sys.version_info < (2, 7):
                children_len = len(item.getchildren())
            else:
                children_len = len(item)

            if children_len > 0:
                xmldict[name] = _xml_to_dict(item)
            else:
                xmldict[name] = item.text
        else:
            if type(xmldict[name]) is not list:
                tempvar = xmldict[name]
                xmldict[name] = []
                xmldict[name].append(tempvar)
            xmldict[name].append(_xml_to_dict(item))
    return xmldict


def query(params=None, setname=None, requesturl=None, location=None,
          return_url=False, return_root=False, endpoint_provider='ec2'):

    provider = get_configured_provider()
    service_url = provider.get('service_url', 'amazonaws.com')

    timestamp = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    if not location:
        location = get_location()

    if not requesturl:
        method = 'GET'

        if endpoint_provider == 'ec2':
            endpoint = provider.get(
                'endpoint',
                'ec2.{0}.{1}'.format(location, service_url)
            )
        elif endpoint_provider == 'elb':
            endpoint = provider.get(
                'elb_endpoint',
                'elasticloadbalancing.amazonaws.com'
            )            
        else:
            log.error(
                'Unknown endpoint_provider: ' + endpoint_provider
            )

        params['AWSAccessKeyId'] = provider['id']
        params['SignatureVersion'] = '2'
        params['SignatureMethod'] = 'HmacSHA256'
        params['Timestamp'] = '{0}'.format(timestamp)
        params['Version'] = '2012-06-01'
        keys = sorted(params.keys())
        values = map(params.get, keys)
        querystring = urllib.urlencode(list(zip(keys, values)))

        uri = '{0}\n{1}\n/\n{2}'.format(method.encode('utf-8'),
                                        endpoint.encode('utf-8'),
                                        querystring.encode('utf-8'))

        hashed = hmac.new(provider['key'], uri, hashlib.sha256)
        sig = binascii.b2a_base64(hashed.digest())
        params['Signature'] = sig.strip()

        querystring = urllib.urlencode(params)
        requesturl = 'https://{0}/?{1}'.format(endpoint, querystring)

    log.debug('EC2 Request: {0}'.format(requesturl))
    try:
        result = urllib2.urlopen(requesturl)
        log.debug(
            'EC2 Response Status Code: {0}'.format(
                result.getcode()
            )
        )
    except urllib2.URLError as exc:
        log.error(
            'EC2 Response Status Code: {0} {1}'.format(
                exc.code, exc.msg
            )
        )
        root = ET.fromstring(exc.read())
        data = _xml_to_dict(root)
        if return_url is True:
            return {'error': data}, requesturl
        return {'error': data}

    response = result.read()
    result.close()

    root = ET.fromstring(response)
    items = root[1]
    if return_root is True:
        items = root

    if setname:
        if sys.version_info < (2, 7):
            children_len = len(root.getchildren())
        else:
            children_len = len(root)

        for item in range(0, children_len):
            comps = root[item].tag.split('}')
            if comps[1] == setname:
                items = root[item]

    ret = []
    for item in items:
        ret.append(_xml_to_dict(item))

    if return_url is True:
        return ret, requesturl

    return ret


def avail_sizes():
    '''
    Return a dict of all available VM images on the cloud provider with
    relevant data. Latest version can be found at:

    http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-types.html
    '''
    sizes = {
        'Cluster Compute': {
            'cc2.8xlarge': {
                'id': 'cc2.8xlarge',
                'cores': '16 (2 x Intel Xeon E5-2670, eight-core with '
                         'hyperthread)',
                'disk': '3360 GiB (4 x 840 GiB)',
                'ram': '60.5 GiB'
            },
            'cc1.4xlarge': {
                'id': 'cc1.4xlarge',
                'cores': '8 (2 x Intel Xeon X5570, quad-core with '
                         'hyperthread)',
                'disk': '1690 GiB (2 x 840 GiB)',
                'ram': '22.5 GiB'
            },
        },
        'Cluster CPU': {
            'cg1.4xlarge': {
                'id': 'cg1.4xlarge',
                'cores': '8 (2 x Intel Xeon X5570, quad-core with '
                         'hyperthread), plus 2 NVIDIA Tesla M2050 GPUs',
                'disk': '1680 GiB (2 x 840 GiB)',
                'ram': '22.5 GiB'
            },
        },
        'High CPU': {
            'c1.xlarge': {
                'id': 'c1.xlarge',
                'cores': '8 (with 2.5 ECUs each)',
                'disk': '1680 GiB (4 x 420 GiB)',
                'ram': '8 GiB'
            },
            'c1.medium': {
                'id': 'c1.medium',
                'cores': '2 (with 2.5 ECUs each)',
                'disk': '340 GiB (1 x 340 GiB)',
                'ram': '1.7 GiB'
            },
        },
        'High I/O': {
            'hi1.4xlarge': {
                'id': 'hi1.4xlarge',
                'cores': '8 (with 4.37 ECUs each)',
                'disk': '2 TiB',
                'ram': '60.5 GiB'
            },
        },
        'High Memory': {
            'm2.2xlarge': {
                'id': 'm2.2xlarge',
                'cores': '4 (with 3.25 ECUs each)',
                'disk': '840 GiB (1 x 840 GiB)',
                'ram': '34.2 GiB'
            },
            'm2.xlarge': {
                'id': 'm2.xlarge',
                'cores': '2 (with 3.25 ECUs each)',
                'disk': '410 GiB (1 x 410 GiB)',
                'ram': '17.1 GiB'
            },
            'm2.4xlarge': {
                'id': 'm2.4xlarge',
                'cores': '8 (with 3.25 ECUs each)',
                'disk': '1680 GiB (2 x 840 GiB)',
                'ram': '68.4 GiB'
            },
        },
        'High-Memory Cluster': {
            'cr1.8xlarge': {
                'id': 'cr1.8xlarge',
                'cores': '16 (2 x Intel Xeon E5-2670, eight-core)',
                'disk': '240 GiB (2 x 120 GiB SSD)',
                'ram': '244 GiB'
            },
        },
        'High Storage': {
            'hs1.8xlarge': {
                'id': 'hs1.8xlarge',
                'cores': '16 (8 cores + 8 hyperthreads)',
                'disk': '48 TiB (24 x 2 TiB hard disk drives)',
                'ram': '117 GiB'
            },
        },
        'Micro': {
            't1.micro': {
                'id': 't1.micro',
                'cores': '1',
                'disk': 'EBS',
                'ram': '615 MiB'
            },
        },
        'Standard': {
            'm1.xlarge': {
                'id': 'm1.xlarge',
                'cores': '4 (with 2 ECUs each)',
                'disk': '1680 GB (4 x 420 GiB)',
                'ram': '15 GiB'
            },
            'm1.large': {
                'id': 'm1.large',
                'cores': '2 (with 2 ECUs each)',
                'disk': '840 GiB (2 x 420 GiB)',
                'ram': '7.5 GiB'
            },
            'm1.medium': {
                'id': 'm1.medium',
                'cores': '1',
                'disk': '400 GiB',
                'ram': '3.75 GiB'
            },
            'm1.small': {
                'id': 'm1.small',
                'cores': '1',
                'disk': '150 GiB',
                'ram': '1.7 GiB'
            },
            'm3.2xlarge': {
                'id': 'm3.2xlarge',
                'cores': '8 (with 3.25 ECUs each)',
                'disk': 'EBS',
                'ram': '30 GiB'
            },
            'm3.xlarge': {
                'id': 'm3.xlarge',
                'cores': '4 (with 3.25 ECUs each)',
                'disk': 'EBS',
                'ram': '15 GiB'
            },
        }
    }
    return sizes


def avail_images():
    '''
    Return a dict of all available VM images on the cloud provider.
    '''
    ret = {}
    params = {'Action': 'DescribeImages'}
    images = query(params)
    for image in images:
        ret[image['imageId']] = image
    return ret


def script(vm_):
    '''
    Return the script deployment object
    '''
    return saltcloud.utils.os_script(
        config.get_config_value('script', vm_, __opts__),
        vm_,
        __opts__,
        saltcloud.utils.salt_config_to_yaml(
            saltcloud.utils.minion_config(__opts__, vm_)
        )
    )


def keyname(vm_):
    '''
    Return the keyname
    '''
    return config.get_config_value(
        'keyname', vm_, __opts__, search_global=False
    )


def securitygroup(vm_):
    '''
    Return the security group
    '''
    return config.get_config_value(
        'securitygroup', vm_, __opts__, search_global=False
    )


def ssh_username(vm_):
    '''
    Return the ssh_username. Defaults to a built-in list of users for trying.
    '''
    usernames = config.get_config_value(
        'ssh_username', vm_, __opts__
    )

    if not isinstance(usernames, list):
        usernames = [usernames]

    # get rid of None's or empty names
    usernames = filter(lambda x: x, usernames)
    # Keep a copy of the usernames the user might have provided
    initial = usernames[:]

    # Add common usernames to the list to be tested
    for name in ('ec2-user', 'ubuntu', 'admin', 'bitnami', 'root'):
        if name not in usernames:
            usernames.append(name)
    # Add the user provided usernames to the end of the list since enough time
    # might need to pass before the remote service is available for logins and
    # the proper username might have passed it's iteration.
    # This has detected in a CentOS 5.7 EC2 image
    usernames.extend(initial)
    return usernames


def ssh_interface(vm_):
    '''
    Return the ssh_interface type to connect to. Either 'public_ips' (default)
    or 'private_ips'.
    '''
    return config.get_config_value(
        'ssh_interface', vm_, __opts__, default='public_ips',
        search_global=False
    )


def get_location(vm_=None):
    '''
    Return the EC2 region to use, in this order:
        - CLI parameter
        - VM parameter
        - Cloud profile setting
    '''
    return __opts__.get(
        'location',
        config.get_config_value(
            'location',
            vm_ or get_configured_provider(),
            __opts__,
            default=DEFAULT_LOCATION,
            search_global=False
        )
    )


def avail_locations():
    '''
    List all available locations
    '''
    ret = {}

    params = {'Action': 'DescribeRegions'}
    result = query(params)

    for region in result:
        ret[region['regionName']] = {
            'name': region['regionName'],
            'endpoint': region['regionEndpoint'],
        }

    return ret


def get_availability_zone(vm_):
    '''
    Return the availability zone to use
    '''
    avz = config.get_config_value(
        'availability_zone', vm_, __opts__, search_global=False
    )

    if avz is None:
        return None

    zones = list_availability_zones()

    # Validate user-specified AZ
    if avz not in zones.keys():
        raise SaltCloudException(
            'The specified availability zone isn\'t valid in this region: '
            '{0}\n'.format(
                avz
            )
        )

    # check specified AZ is available
    elif zones[avz] != 'available':
        raise SaltCloudException(
            'The specified availability zone isn\'t currently available: '
            '{0}\n'.format(
                avz
            )
        )

    return avz


def get_subnetid(vm_):
    '''
    Returns the SubnetId to use
    '''
    subnetid = config.get_config_value(
        'subnetid', vm_, __opts__, search_global=False
    )
    if subnetid is None:
        return None
    return subnetid


def securitygroupid(vm_):
    '''
    Returns the SecurityGroupId
    '''
    return config.get_config_value(
        'securitygroupid', vm_, __opts__, search_global=False
    )


def list_availability_zones():
    '''
    List all availability zones in the current region
    '''
    ret = {}

    params = {'Action': 'DescribeAvailabilityZones',
              'Filter.0.Name': 'region-name',
              'Filter.0.Value.0': get_location()}
    result = query(params)

    for zone in result:
        ret[zone['zoneName']] = zone['zoneState']

    return ret


def create(vm_=None, call=None):
    '''
    Create a single VM from a data dict
    '''
    if call:
        raise SaltCloudSystemExit(
            'You cannot create an instance with -a or -f.'
        )

    key_filename = config.get_config_value(
        'private_key', vm_, __opts__, search_global=False, default=None
    )
    if key_filename is not None and not os.path.isfile(key_filename):
        raise SaltCloudConfigError(
            'The defined key_filename {0!r} does not exist'.format(
                key_filename
            )
        )

    location = get_location(vm_)
    log.info('Creating Cloud VM {0} in {1}'.format(vm_['name'], location))
    usernames = ssh_username(vm_)
    params = {'Action': 'RunInstances',
              'MinCount': '1',
              'MaxCount': '1'}
    params['ImageId'] = vm_['image']

    vm_size = config.get_config_value(
        'size', vm_, __opts__, search_global=False
    )
    if vm_size in SIZE_MAP:
        params['InstanceType'] = SIZE_MAP[vm_size]
    else:
        params['InstanceType'] = vm_size
    ex_keyname = keyname(vm_)
    if ex_keyname:
        params['KeyName'] = ex_keyname
    ex_securitygroup = securitygroup(vm_)
    if ex_securitygroup:
        if not isinstance(ex_securitygroup, list):
            params['SecurityGroup.1'] = ex_securitygroup
        else:
            for (counter, sg_) in enumerate(ex_securitygroup):
                params['SecurityGroup.{0}'.format(counter)] = sg_

    az_ = get_availability_zone(vm_)
    if az_ is not None:
        params['Placement.AvailabilityZone'] = az_

    subnetid_ = get_subnetid(vm_)
    if subnetid_ is not None:
        params['SubnetId'] = subnetid_

    ex_securitygroupid = securitygroupid(vm_)
    if ex_securitygroupid:
        if not isinstance(ex_securitygroupid, list):
            params['SecurityGroupId.1'] = ex_securitygroupid
        else:
            for (counter, sg_) in enumerate(ex_securitygroupid):
                params['SecurityGroupId.{0}'.format(counter)] = sg_

    set_delvol_on_destroy = config.get_config_value(
        'delvol_on_destroy', vm_, __opts__, search_global=False
    )

    if set_delvol_on_destroy is not None:
        if not isinstance(set_delvol_on_destroy, bool):
            raise SaltCloudConfigError(
                '\'delvol_on_destroy\' should be a boolean value.'
            )

        params['BlockDeviceMapping.1.DeviceName'] = '/dev/sda1'
        params['BlockDeviceMapping.1.Ebs.DeleteOnTermination'] = str(
            set_delvol_on_destroy
        ).lower()

    # Get ANY defined volumes settings, merging data, in the following order
    # 1. VM config
    # 2. Profile config
    # 3. Global configuration
    volumes = config.get_config_value(
        'volumes', vm_, __opts__, search_global=True
    )

    if volumes:
        ephemerals = [vol for vol in volumes if 'virtualname' in vol]
        volumes = [ vol for vol in volumes if 'virtualname' not in vol]
        if ephemerals:
            device_index = 2
            for vol in ephemerals:
                params['BlockDeviceMapping.{0}.DeviceName'.format(device_index)] = vol['device']
                params['BlockDeviceMapping.{0}.VirtualName'.format(device_index)] = vol['virtualname']
                device_index += 1

    ex_userdata = userdata(vm_)
    if ex_userdata:
        log.info('Applying user data script')
        params['UserData'] = base64.b64encode(ex_userdata)

    try:
        data = query(params, 'instancesSet', location=location)
        if 'error' in data:
            return data['error']
    except Exception as exc:
        log.error(
            'Error creating {0} on EC2 when trying to run the initial '
            'deployment: \n{1}'.format(
                vm_['name'], exc
            ),
            # Show the traceback if the debug logging level is enabled
            exc_info=log.isEnabledFor(logging.DEBUG)
        )
        raise

    instance_id = data[0]['instanceId']

    log.debug('The new VM instance_id is {0}'.format(instance_id))

    params = {'Action': 'DescribeInstances',
              'InstanceId.1': instance_id}

    attempts = 5
    while attempts > 0:
        data, requesturl = query(params, location=location, return_url=True)
        log.debug('The query returned: {0}'.format(data))

        if isinstance(data, dict) and 'error' in data:
            log.warn(
                'There was an error in the query. {0} attempts '
                'remaining: {1}'.format(
                    attempts, data['error']
                )
            )
            attempts -= 1
            continue

        if isinstance(data, list) and not data:
            log.warn(
                'There was an error in the query. {0} attempts '
                'remaining: {1}'.format(
                    attempts, data
                )
            )
            attempts -= 1
            continue

        break
    else:
        raise SaltCloudSystemExit(
            'An error occurred while creating VM: {0}'.format(data['error'])
        )

    def __query_ip_address(params, url):
        data = query(params, requesturl=url)
        if not data:
            log.error(
                'There was an error while querying EC2. Empty response'
            )
            # Trigger a failure in the wait for IP function
            return False

        if isinstance(data, dict) and 'error' in data:
            log.warn(
                'There was an error in the query. {0}'.format(data['error'])
            )
            # Trigger a failure in the wait for IP function
            return False

        log.debug('Returned query data: {0}'.format(data))

        if 'ipAddress' in data[0]['instancesSet']['item']:
            return data
        if 'privateIpAddress' in data[0]['instancesSet']['item']:
            return data

    try:
        data = saltcloud.utils.wait_for_ip(
            __query_ip_address,
            update_args=(params, requesturl),
        )
    except (SaltCloudExecutionTimeout, SaltCloudExecutionFailure) as exc:
        try:
            # It might be already up, let's destroy it!
            destroy(vm_['name'])
        except SaltCloudSystemExit:
            pass
        finally:
            raise SaltCloudSystemExit(exc.message)

    set_tags(
        vm_['name'], {'Name': vm_['name']},
        instance_id=instance_id, call='action', location=location
    )
    log.info('Created node {0}'.format(vm_['name']))

    if ssh_interface(vm_) == 'private_ips':
        ip_address = data[0]['instancesSet']['item']['privateIpAddress']
        log.info('Salt node data. Private_ip: {0}'.format(ip_address))
    else:
        ip_address = data[0]['instancesSet']['item']['ipAddress']
        log.info('Salt node data. Public_ip: {0}'.format(ip_address))

    ret = {}
    if not ex_userdata: # TODO: make this less hacky, it is too speciialized for the windows scenario
        display_ssh_output = config.get_config_value(
            'display_ssh_output', vm_, __opts__, default=True
        )

        if config.get_config_value('deploy', vm_, __opts__) is True:
            if saltcloud.utils.wait_for_ssh(ip_address):
                for user in usernames:
                    if saltcloud.utils.wait_for_passwd(
                        host=ip_address,
                        username=user,
                        ssh_timeout=60,
                        key_filename=key_filename,
                        display_ssh_output=display_ssh_output
                    ):
                        username = user
                        break
                else:
                    raise SaltCloudSystemExit(
                        'Failed to authenticate against remote ssh'
                    )

            deploy_script = script(vm_)
            deploy_kwargs = {
                'host': ip_address,
                'username': username,
                'key_filename': key_filename,
                'deploy_command': '/tmp/deploy.sh',
                'tty': True,
                'script': deploy_script,
                'name': vm_['name'],
                'sudo': config.get_config_value(
                    'sudo', vm_, __opts__, default=(username != 'root')
                ),
                'start_action': __opts__['start_action'],
                'parallel': __opts__['parallel'],
                'conf_file': __opts__['conf_file'],
                'sock_dir': __opts__['sock_dir'],
                'minion_pem': vm_['priv_key'],
                'minion_pub': vm_['pub_key'],
                'keep_tmp': __opts__['keep_tmp'],
                'preseed_minion_keys': vm_.get('preseed_minion_keys', None),
                'display_ssh_output': display_ssh_output,
                'minion_conf': saltcloud.utils.minion_config(__opts__, vm_),
                'script_args': config.get_config_value(
                    'script_args', vm_, __opts__
                ),
                'script_env': config.get_config_value(
                    'script_env', vm_, __opts__
                )
            }

            # Deploy salt-master files, if necessary
            if config.get_config_value('make_master', vm_, __opts__) is True:
                deploy_kwargs['make_master'] = True
                deploy_kwargs['master_pub'] = vm_['master_pub']
                deploy_kwargs['master_pem'] = vm_['master_pem']
                master_conf = saltcloud.utils.master_config(__opts__, vm_)
                deploy_kwargs['master_conf'] = master_conf

                if master_conf.get('syndic_master', None):
                    deploy_kwargs['make_syndic'] = True

            deploy_kwargs['make_minion'] = config.get_config_value(
                'make_minion', vm_, __opts__, default=True
            )

            ret['deploy_kwargs'] = deploy_kwargs
            deployed = saltcloud.utils.deploy_script(**deploy_kwargs)
            if deployed:
                log.info('Salt installed on {name}'.format(**vm_))
            else:
                log.error('Failed to start Salt on Cloud VM {name}'.format(**vm_))
    else:
        log.info('Administrator password not yet generated.  Check back later with \'salt-cloud -a get_password {0}\''.format(vm_['name']))
 
    log.info('Created Cloud VM {0[name]!r}'.format(vm_))
    log.debug(
        '{0[name]!r} VM creation details:\n{1}'.format(
            vm_, pprint.pformat(data[0]['instancesSet']['item'])
        )
    )

    ret.update(data[0]['instancesSet']['item'])

    if volumes:
        log.info('Create and attach volumes to node {0}'.format(vm_['name']))
        created = create_attach_volumes(
            vm_['name'],
            {
                'volumes': volumes,
                'zone': ret['placement']['availabilityZone'],
                'instance_id': ret['instanceId']
            },
            call='action'
        )
        ret['Attached Volumes'] = created

    return ret


def create_attach_volumes(name, kwargs, call=None):
    '''
    Create and attach volumes to created node
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The create_attach_volumes action must be called with -a or --action.'
        )

    if not 'instance_id' in kwargs:
        kwargs['instance_id'] = _get_node(name)['instanceId']

    if type(kwargs['volumes']) is str:
        volumes = yaml.safe_load(kwargs['volumes'])
    else:
        volumes = kwargs['volumes']

    ret = []
    for volume in volumes:
        volume_name = '{0} on {1}'.format(volume['device'], name)

        volume_dict = {
            'volume_name': volume_name,
            'zone': kwargs['zone']
        }
        if 'volume_id' in volume:
            volume_dict['volume_id'] = volume['volume_id']
        elif 'snapshot' in volume:
            volume_dict['snapshot'] = volume['snapshot']
        else:
            volume_dict['size'] = volume['size']

            if 'type' in volume:
                volume_dict['type'] = volume['type']
            if 'iops' in volume:
                volume_dict['iops'] = volume['iops']

        if 'volume_id' not in volume_dict:
            created_volume = create_volume(volume_dict, call='function')
            for item in created_volume:
                if 'volumeId' in item:
                    volume_dict['volume_id'] = item['volumeId']

        # TODO: this might be broken post merge
        attempts = 5
        while attempts > 0:
            data = attach_volume(
                name,
                {'volume_id': volume_dict['volume_id'], 'device': volume['device']},
                instance_id=kwargs['instance_id'],
                call='action'
                )
            log.debug('The query returned: {0}'.format(data))

            if isinstance(data, dict) and 'error' in data:
                log.warn(
                    'There was an error in the query. {0} attempts '
                    'remaining: {1}'.format(
                        attempts, data['error']
                    )
                )
                attempts -= 1
		sleep(1)
                continue

            if isinstance(data, list) and not data:
                log.warn(
                    'There was an error in the query. {0} attempts '
                    'remaining: {1}'.format(
                        attempts, data['error']
                    )
                )
                attempts -= 1
		sleep(1)
                continue
            
            # No errors, volume successfully attached

            msg = (
                '{0} attached to {1} (aka {2}) as device {3}'.format(
                    volume_dict['volume_id'], kwargs['instance_id'], name, volume['device']
                )
            )
            log.info(msg)
            ret.append(msg)
            break
        else:
            raise SaltCloudSystemExit(
                'An error occurred while creating VM: {0}'.format(data['error'])
            )

    return ret

def create_attach_volumes_quick(name, kwargs, call=None):
    '''
    Create and attach volumes to created node
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The create_attach_volumes_quick action must be called with -a or --action.'
        )

    if not 'drive_letters' in kwargs:
        log.error('drive_letters must be specified')
        return False

    if not 'size' in kwargs:
        log.error('size must be specified')
        return False        

    volumes = []
    for letter in kwargs['drive_letters']:
        volume = {}
        volume['size'] = kwargs['size']
        volume['device'] = '/dev/sd' + letter
        volumes.append(volume)
        
    instance = _get_node(name)

    return create_attach_volumes(name,
                                {
                                    'volumes': volumes,
                                    'instance_id': instance['instanceId'],
                                    'zone': instance['placement']['availabilityZone']
                                },
                                call='action'
                            )
    
    
    

def stop(name, call=None):
    '''
    Stop a node
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The stop action must be called with -a or --action.'
        )

    log.info('Stopping node {0}'.format(name))

    instance_id = _get_node(name)['instanceId']

    params = {'Action': 'StopInstances',
              'InstanceId.1': instance_id}
    result = query(params)

    return result


def start(name, call=None):
    '''
    Start a node
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The start action must be called with -a or --action.'
        )

    log.info('Starting node {0}'.format(name))

    instance_id = _get_node(name)['instanceId']

    params = {'Action': 'StartInstances',
              'InstanceId.1': instance_id}
    result = query(params)

    return result


def set_tags(name, tags, call=None, location=None, instance_id=None):
    '''
    Set tags for a node

    CLI Example::

        salt-cloud -a set_tags mymachine tag1=somestuff tag2='Other stuff'
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The set_tags action must be called with -a or --action.'
        )

    if instance_id is None:
        instance_id = _get_node(name, location)['instanceId']

    params = {'Action': 'CreateTags',
              'ResourceId.1': instance_id}

    log.debug('Tags to set for {0}: {1}'.format(name, tags))

    for idx, (tag_k, tag_v) in enumerate(tags.iteritems()):
        params['Tag.{0}.Key'.format(idx)] = tag_k
        params['Tag.{0}.Value'.format(idx)] = tag_v

    attempts = 5
    while attempts >= 0:
        query(params, setname='tagSet', location=location)

        settags = get_tags(
            instance_id=instance_id, call='action', location=location
        )

        log.debug('Setting the tags returned: {0}'.format(settags))

        failed_to_set_tags = False
        for tag in settags:
            if tag['key'] not in tags:
                # We were not setting this tag
                continue

            if tags.get(tag['key']) != tag['value']:
                # Not set to the proper value!?
                failed_to_set_tags = True
                break

        if failed_to_set_tags:
            log.warn(
                'Failed to set tags. Remaining attempts {0}'.format(
                    attempts
                )
            )
            attempts -= 1
            continue

        return settags

    raise SaltCloudSystemExit(
        'Failed to set tags on {0}!'.format(name)
    )


def get_tags(name=None, instance_id=None, call=None, location=None):
    '''
    Retrieve tags for a node
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The get_tags action must be called with -a or --action.'
        )

    if instance_id is None:
        instances = list_nodes_full(location)
        if name in instances:
            instance_id = instances[name]['instanceId']

    params = {'Action': 'DescribeTags',
              'Filter.1.Name': 'resource-id',
              'Filter.1.Value': instance_id}
    return query(params, setname='tagSet', location=location)


def del_tags(name, kwargs, call=None):
    '''
    Delete tags for a node

    CLI Example::

        salt-cloud -a del_tags mymachine tag1,tag2,tag3
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The del_tags action must be called with -a or --action.'
        )

    if not 'tags' in kwargs:
        raise SaltCloudSystemExit(
            'A tag or tags must be specified using tags=list,of,tags'
        )

    instance_id = _get_node(name)['instanceId']
    params = {'Action': 'DeleteTags',
              'ResourceId.1': instance_id}

    for idx, tag in enumerate(kwargs['tags'].split(',')):
        params['Tag.{0}.Key'.format(idx)] = tag

    query(params, setname='tagSet')

    return get_tags(name, call='action')


def rename(name, kwargs, call=None):
    '''
    Properly rename a node. Pass in the new name as "new name".

    CLI Example::

        salt-cloud -a rename mymachine newname=yourmachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The rename action must be called with -a or --action.'
        )

    log.info('Renaming {0} to {1}'.format(name, kwargs['newname']))

    set_tags(name, {'Name': kwargs['newname']}, call='action')

    saltcloud.utils.rename_key(
        __opts__['pki_dir'], name, kwargs['newname']
    )


def destroy(name, call=None):
    '''
    Destroy a node. Will check termination protection and warn if enabled.

    CLI Example::

        salt-cloud --destroy mymachine
    '''
    instance_id = _get_node(name)['instanceId']
    protected = show_term_protect(
        name=name,
        instance_id=instance_id,
        call='action',
        quiet=True
    )

    if protected == 'true':
        raise SaltCloudSystemExit(
            'This instance has been protected from being destroyed. '
            'Use the following command to disable protection:\n\n'
            'salt-cloud -a disable_term_protect {0}'.format(
                name
            )
        )

    ret = {}

    if config.get_config_value('rename_on_destroy',
                               get_configured_provider(),
                               __opts__, search_global=False) is True:
        newname = '{0}-DEL{1}'.format(name, uuid.uuid4().hex)
        rename(name, kwargs={'newname': newname}, call='action')
        log.info(
            'Machine will be identified as {0} until it has been '
            'cleaned up.'.format(
                newname
            )
        )
        ret['newname'] = newname

    params = {'Action': 'TerminateInstances',
              'InstanceId.1': instance_id}
    result = query(params)
    log.info(result)

    ret.update(result[0])
    return ret


def reboot(name, call=None):
    '''
    Reboot a node.

    CLI Example::

        salt-cloud -a reboot mymachine
    '''
    instance_id = _get_node(name)['instanceId']
    params = {'Action': 'RebootInstances',
              'InstanceId.1': instance_id}
    result = query(params)
    if result == []:
        log.info("Complete")

    return {'Reboot': 'Complete'}


def show_image(kwargs, call=None):
    '''
    Show the details from EC2 concerning an AMI
    '''
    if call != 'function':
        raise SaltCloudSystemExit(
            'The show_image action must be called with -f or --function.'
        )

    params = {'ImageId.1': kwargs['image'],
              'Action': 'DescribeImages'}
    result = query(params)
    log.info(result)

    return result


def show_instance(name, call=None):
    '''
    Show the details from EC2 concerning an AMI
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_instance action must be called with -a or --action.'
        )

    return _get_node(name)


def _get_node(name, location=None):
    attempts = 10
    while attempts >= 0:
        try:
            return list_nodes_full(location)[name]
        except KeyError:
            attempts -= 1
            log.debug(
                'Failed to get the data for the node {0!r}. Remaining '
                'attempts {1}'.format(
                    name, attempts
                )
            )
            # Just a little delay between attempts...
            time.sleep(0.5)
    return {}


def list_nodes_full(location=None):
    '''
    Return a list of the VMs that are on the provider
    '''
    if not location:
        ret = {}
        locations = set(
            get_location(vm_) for vm_ in __opts__['profiles'].values()
            if _vm_provider_driver(vm_)
        )
        if len(locations) == 0:
            locations = set([ get_location() ])
        for loc in locations:
            ret.update(_list_nodes_full(loc))
        return ret
    
    return _list_nodes_full(location)


def _vm_provider_driver(vm_):
    alias, driver = vm_['provider'].split(':')
    if alias not in __opts__['providers']:
        return None

    if driver not in __opts__['providers'][alias]:
        return None

    return vm_['provider'] == (__active_provider_name__ or 'ec2')


def _extract_name_tag(item):
    if 'tagSet' in item:
        tagset = item['tagSet']
        if type(tagset['item']) is list:
            for tag in tagset['item']:
                if tag['key'] == 'Name':
                    return tag['value']
            return item['instanceId']
        return (item['tagSet']['item']['value'])
    return item['instanceId']


def _list_nodes_full(location=None):
    '''
    Return a list of the VMs that in this location
    '''

    ret = {}
    params = {'Action': 'DescribeInstances'}
    instances = query(params, location=location)
    if 'error' in instances:
        raise SaltCloudSystemExit(
            'An error occurred while listing nodes: {0}'.format(
                instances['error']['Errors']['Error']['Message']
            )
        )

    for instance in instances:
        # items could be type dict or list (for stopped EC2 instances)
        if isinstance(instance['instancesSet']['item'], list):
            for item in instance['instancesSet']['item']:
                name = _extract_name_tag(item)
                ret[name] = item
                ret[name].update(
                    dict(
                        id=item['instanceId'],
                        image=item['imageId'],
                        size=item['instanceType'],
                        state=item['instanceState']['name'],
                        private_ips=item.get('privateIpAddress', []),
                        public_ips=item.get('ipAddress', [])
                    )
                )
        else:
            item = instance['instancesSet']['item']
            name = _extract_name_tag(item)
            ret[name] = item
            ret[name].update(
                dict(
                    id=item['instanceId'],
                    image=item['imageId'],
                    size=item['instanceType'],
                    state=item['instanceState']['name'],
                    private_ips=item.get('privateIpAddress', []),
                    public_ips=item.get('ipAddress', [])
                )
            )
    return ret


def list_nodes():
    '''
    Return a list of the VMs that are on the provider
    '''
    ret = {}
    nodes = list_nodes_full()
    if 'error' in nodes:
        raise SaltCloudSystemExit(
            'An error occurred while listing nodes: {0}'.format(
                nodes['error']['Errors']['Error']['Message']
            )
        )
    for node in nodes:
        ret[node] = {
            'id': nodes[node]['id'],
            'image': nodes[node]['image'],
            'size': nodes[node]['size'],
            'state': nodes[node]['state'],
            'private_ips': nodes[node]['private_ips'],
            'public_ips': nodes[node]['public_ips'],
        }
    return ret


def list_nodes_select():
    '''
    Return a list of the VMs that are on the provider, with select fields
    '''
    ret = {}

    nodes = list_nodes_full()
    if 'error' in nodes:
        raise SaltCloudSystemExit(
            'An error occurred while listing nodes: {0}'.format(
                nodes['error']['Errors']['Error']['Message']
            )
        )

    for node in nodes:
        pairs = {}
        data = nodes[node]
        for key in data:
            if str(key) in __opts__['query.selection']:
                value = data[key]
                pairs[key] = value
        ret[node] = pairs

    return ret


def show_term_protect(name=None, instance_id=None, call=None, quiet=False):
    '''
    Show the details from EC2 concerning an AMI
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_term_protect action must be called with -a or --action.'
        )

    if not instance_id:
        instances = list_nodes_full()
        instance_id = instances[name]['instanceId']
    params = {'Action': 'DescribeInstanceAttribute',
              'InstanceId': instance_id,
              'Attribute': 'disableApiTermination'}
    result = query(params, return_root=True)

    disable_protect = False
    for item in result:
        if 'value' in item:
            disable_protect = item['value']
            break

    log.log(
        logging.DEBUG if quiet is True else logging.INFO,
        'Termination Protection is {0} for {1}'.format(
            disable_protect == 'true' and 'enabled' or 'disabled',
            name
        )
    )

    return disable_protect


def enable_term_protect(name, call=None):
    '''
    Enable termination protection on a node

    CLI Example::

        salt-cloud -a enable_term_protect mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The enable_term_protect action must be called with '
            '-a or --action.'
        )

    return _toggle_term_protect(name, 'true')


def disable_term_protect(name, call=None):
    '''
    Disable termination protection on a node

    CLI Example::

        salt-cloud -a disable_term_protect mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The disable_term_protect action must be called with '
            '-a or --action.'
        )

    return _toggle_term_protect(name, 'false')


def _toggle_term_protect(name, value):
    '''
    Disable termination protection on a node

    CLI Example::

        salt-cloud -a disable_term_protect mymachine
    '''
    instances = list_nodes_full()
    instance_id = instances[name]['instanceId']
    params = {'Action': 'ModifyInstanceAttribute',
              'InstanceId': instance_id,
              'DisableApiTermination.Value': value}

    query(params, return_root=True)

    return show_term_protect(name=name, instance_id=instance_id, call='action')

def show_sourcedest_check(name=None, instance_id=None, call=None, quiet=False):
    '''
    Show the details from EC2 concerning an AMI
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The show_sourcedest_check action must be called with -a or --action.'
        )

    if not instance_id:
        instances = list_nodes_full()
        instance_id = instances[name]['instanceId']
    params = {'Action': 'DescribeInstanceAttribute',
              'InstanceId': instance_id,
              'Attribute': 'sourceDestCheck'}
    result = query(params, return_root=True)

    sourcedest_check = False
    for item in result:
        if 'value' in item:
            sourcedest_check = item['value']
            break

    log.log(
        logging.DEBUG if quiet is True else logging.INFO,
        'Source/Destination Check is {0} for {1}'.format(
            sourcedest_check == 'true' and 'enabled' or 'disabled',
            name
        )
    )

    return sourcedest_check


def enable_sourcedest_check(name, call=None):
    '''
    Enable the source/destination check on a machine

    CLI Example::

        salt-cloud -a enable_sourcedest_check mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The enable_sourcedest_check action must be called with '
            '-a or --action.'
        )

    return _toggle_sourcedest_check(name, 'true')


def disable_sourcedest_check(name, call=None):
    '''
    Disable the source/destination check on a machine

    CLI Example::

        salt-cloud -a disable_sourcedest_check mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The disable_sourcedest_check action must be called with '
            '-a or --action.'
        )

    return _toggle_sourcedest_check(name, 'false')

def _toggle_sourcedest_check(name, value):
    '''
    Toggle source/destination check on a node

    CLI Example::

        salt-cloud -a enable_sourcedest_check mymachine
        salt-cloud -a disable_sourcedest_check mymachine
    '''
    instances = list_nodes_full()
    instance_id = instances[name]['instanceId']
    params = {'Action': 'ModifyInstanceAttribute',
              'InstanceId': instance_id,
              'SourceDestCheck.Value': value}

    query(params, return_root=True)

    return show_sourcedest_check(name=name, instance_id=instance_id, call='action')

def describe_instance(name=None, instance_id=None, call=None):
    '''
    Show the details from EC2 of an existing instance
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The describe_instance action must be called with -a or --action.'
        )

    if not instance_id:
        instances = list_nodes_full()
        instance_id = instances[name]['instanceId']
    params = {'Action': 'DescribeInstances',
              'InstanceId.1': instance_id }
    result = query(params, return_root=True)
    return result

def keepvol_on_destroy(name, call=None):
    '''
    Do not delete root EBS volume upon instance termination

    CLI Example::

        salt-cloud -a keepvol_on_destroy mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The keepvol_on_destroy action must be called with -a or --action.'
        )

    return _toggle_delvol(name=name, value='false')


def delvol_on_destroy(name, call=None):
    '''
    Delete root EBS volume upon instance termination

    CLI Example::

        salt-cloud -a delvol_on_destroy mymachine
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The delvol_on_destroy action must be called with -a or --action.'
        )

    return _toggle_delvol(name=name, value='true')


def _toggle_delvol(name=None, instance_id=None, value=None, requesturl=None):
    '''
    Disable termination protection on a node

    CLI Example::

        salt-cloud -a disable_term_protect mymachine
    '''
    if not instance_id:
        instances = list_nodes_full()
        instance_id = instances[name]['instanceId']

    if requesturl:
        data = query(requesturl=requesturl)
    else:
        params = {'Action': 'DescribeInstances',
                  'InstanceId.1': instance_id}
        data, requesturl = query(params, return_url=True)

    blockmap = data[0]['instancesSet']['item']['blockDeviceMapping']
    device_name = blockmap['item']['deviceName']

    params = {'Action': 'ModifyInstanceAttribute',
              'InstanceId': instance_id,
              'BlockDeviceMapping.1.DeviceName': device_name,
              'BlockDeviceMapping.1.Ebs.DeleteOnTermination': value}

    query(params, return_root=True)

    return query(requesturl=requesturl)


def create_volume(kwargs=None, call=None):
    '''
    Create a volume
    '''
    if call != 'function':
        log.error(
            'The create_volume function must be called with -f or --function.'
        )
        return False

    if 'zone' not in kwargs:
        log.error('An availability zone must be specified to create a volume.')
        return False

    if 'size' not in kwargs and 'snapshot' not in kwargs:
        # This number represents GiB
        kwargs['size'] = '10'

    params = {'Action': 'CreateVolume',
              'AvailabilityZone': kwargs['zone']}

    if 'size' in kwargs:
        params['Size'] = kwargs['size']

    if 'snapshot' in kwargs:
        params['SnapshotId'] = kwargs['snapshot']

    if 'type' in kwargs:
        params['VolumeType'] = kwargs['type']

    if 'iops' in kwargs and kwargs.get('type', 'standard') == 'io1':
        params['Iops'] = kwargs['iops']

    log.debug(params)

    data = query(params, return_root=True)

    # Wait a few seconds to make sure the volume
    # has had a chance to shift to available state
    # TODO: Should probably create a util method to
    # wait for available status and fail on others
    time.sleep(5)

    return data


def attach_volume(name=None, kwargs=None, instance_id=None, call=None):
    '''
    Attach a volume to an instance
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The attach_volume action must be called with -a or --action.'
        )

    if not kwargs:
        kwargs = {}

    if 'instance_id' in kwargs:
        instance_id = kwargs['instance_id']

    if name and not instance_id:
        instances = list_nodes_full()
        instance_id = instances[name]['instanceId']

    if not name and not instance_id:
        log.error('Either a name or an instance_id is required.')
        return False

    if 'volume_id' not in kwargs:
        log.error('A volume_id is required.')
        return False

    if 'device' not in kwargs:
        log.error('A device is required (ex. /dev/sdb1).')
        return False

    params = {'Action': 'AttachVolume',
              'VolumeId': kwargs['volume_id'],
              'InstanceId': instance_id,
              'Device': kwargs['device']}

    log.debug(params)

    data = query(params, return_root=True)
    return data


def show_volume(name=None, kwargs=None, instance_id=None, call=None):
    '''
    Show volume details
    '''
    if not kwargs:
        kwargs = {}

    if 'volume_id' not in kwargs:
        log.error('A volume_id is required.')
        return False

    params = {'Action': 'DescribeVolumes',
              'VolumeId.1': kwargs['volume_id']}

    data = query(params, return_root=True)
    return data


def detach_volume(name=None, kwargs=None, instance_id=None, call=None):
    '''
    Detach a volume from an instance
    '''
    if call != 'action':
        raise SaltCloudSystemExit(
            'The detach_volume action must be called with -a or --action.'
        )

    if not kwargs:
        kwargs = {}

    if 'volume_id' not in kwargs:
        log.error('A volume_id is required.')
        return False

    params = {'Action': 'DetachVolume',
              'VolumeId': kwargs['volume_id']}

    data = query(params, return_root=True)
    return data


def delete_volume(name=None, kwargs=None, instance_id=None, call=None):
    '''
    Delete a volume
    '''
    if not kwargs:
        kwargs = {}

    if 'volume_id' not in kwargs:
        log.error('A volume_id is required.')
        return False

    params = {'Action': 'DeleteVolume',
              'VolumeId': kwargs['volume_id']}

    data = query(params, return_root=True)
    return data


def create_keypair(kwargs=None, call=None):
    '''
    Create an SSH keypair
    '''
    if call != 'function':
        log.error(
            'The create_keypair function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'keyname' not in kwargs:
        log.error('A keyname is required.')
        return False

    params = {'Action': 'CreateKeyPair',
              'KeyName': kwargs['keyname']}

    data = query(params, return_root=True)
    return data


def show_keypair(kwargs=None, call=None):
    '''
    Show the details of an SSH keypair
    '''
    if call != 'function':
        log.error(
            'The show_keypair function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'keyname' not in kwargs:
        log.error('A keyname is required.')
        return False

    params = {'Action': 'DescribeKeyPairs',
              'KeyName.1': kwargs['keyname']}

    data = query(params, return_root=True)
    return data


def delete_keypair(kwargs=None, call=None):
    '''
    Delete an SSH keypair
    '''
    if call != 'function':
        log.error(
            'The delete_keypair function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'keyname' not in kwargs:
        log.error('A keyname is required.')
        return False

    params = {'Action': 'DeleteKeyPair',
              'KeyName.1': kwargs['keyname']}

    data = query(params, return_root=True)
    return data

def userdata(vm_):
    '''
    Return a string containing the userdata script to run
    '''
    userdata_file = config.get_config_value(
        'userdata', vm_, __opts__, default=None,
        search_global=False
    )
    if userdata_file:
        try:
            minion = saltcloud.utils.minion_config(__opts__, vm_)
            userdata = "\n".join(open(userdata_file).readlines()).replace('%MINION_PUB%', vm_['pub_key']).replace('%MINION_PEM%', vm_['priv_key']).replace('%MINION_ID%', vm_['name']).replace('%MASTER_HOST%', minion['master'])
            return userdata
        except IOError:
            return False
    else:
        return False

def create_elb(kwargs=None, call=None):
    '''
    Create an Elastic Load Balancer
    '''
    if call != 'function':
        log.error(
            'The create_elb function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'zones' not in kwargs and 'subnets' not in kwargs:
        log.error('At least one Availability Zone or SubnetId is required.')
        return False

    if 'listeners' not in kwargs:
        log.error('At least one Listener is required.')
        return False

    params = {'Action': 'CreateLoadBalancer',
              'LoadBalancerName': kwargs['loadbalancername']
              # TODO: fix VPC / scheme
              # 'Scheme': kwargs['scheme']
              }

    # AvailabilityZones
    # zones=us-west-2a;us-west-2b
    if 'zones' in kwargs:
        if not isinstance(kwargs['zones'], list):
            zones = kwargs['zones'].split(';')
        else:
            zones = kwargs['zones']
        for index in range(0, len(zones)):
            params['AvailabilityZones.member.' + str(index+1)] = zones[index]

    # Listeners
    # listeners=protocol=HTTP,lb-port=80,instance-port=80,instance-protocol=HTTP;TCP,lb-port=443,instance-port=443,instance-protocol=TCP
    # TODO: find a better delimeter than ;
    if not isinstance(kwargs['listeners'], list):
        listeners = _parse_str_parameters(kwargs['listeners'])
    else:
        listeners = kwargs['listeners']
    for index in range(0, len(listeners)):
        listener = listeners[index]
        if 'protocol' in listener and 'instance-port' in listener and 'lb-port' in listener:
            params['Listeners.member.{0}.Protocol'.format(index+1)] = listener['protocol']
            params['Listeners.member.{0}.InstancePort'.format(index+1)] = listener['instance-port']
            params['Listeners.member.{0}.LoadBalancerPort'.format(index+1)] = listener['lb-port']
        else:
            log.error('protocol, instance-port, lb-port are required parameters')
            return False
        if 'instance-protocol' in listener: 
            params['Listeners.member.{0}.InstanceProtocol'.format(index+1)] = listener['instance-protocol']
        if 'cert-id' in listener:
            params['Listeners.member.{0}.SSLCertificateId'.format(index+1)] = listener['cert-id']
    # Subnets and Security groups only required for VPC?

    # SecurityGroups
    # securitygroups=http-servers;db-servers;default
    if 'securitygroups' in kwargs:
        if not isinstance(kwargs['securitygroups'], list):
            securitygroups = kwargs['securitygroups'].split(';')
        else:
            securitygroups = kwargs['securitygroups']
        for index in range(0, len(securitygroups)):
            params['SecurityGroups.member.' + str(index+1)] = securitygroups[index]

    # Subnets
    if 'subnets' in kwargs:
        if not isinstance(kwargs['subnets'], list):
            subnets = kwargs['subnets'].split(';')
        else:
            subnets = kwargs['subnets']
        for index in range(0, len(subnets)):
            params['Subnets.member.' + str(index+1)] = subnets[index]

    data = query(params, return_root=True, endpoint_provider='elb')
    return data

def attach_elb(kwargs=None, call=None):
    '''
    Create an Elastic Load Balancer
    '''
    if call != 'function':
        log.error(
            'The create_elb function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'instances' not in kwargs:
        log.error('At least one instance id is required.')
        return False

    params = {'Action': 'RegisterInstancesWithLoadBalancer',
              'LoadBalancerName': kwargs['loadbalancername']
              }

    # Instances.member.N instances=i-184dbd7b;i-b6d5b9dc
    instances = kwargs['instances'].split(';')
    for index in range(0, len(instances)):
        params['Instances.member.{0}.InstanceId'.format(index+1)] = instances[index]

    data = query(params, return_root=True, endpoint_provider='elb')
    return data

def configure_elb_healthcheck(kwargs=None, call=None):
    '''
    Configure the Health Check on a pre-existing Elastic Load Balancer
    '''
    if call != 'function':
        log.error(
            'The create_elb function must be called with -f or --function.'
            )
        return False

    if not kwargs:
        kwargs = {}

    if 'healthythreshold' not in kwargs:
        log.error('You must specifiy a healthythreshhold')
        return False

    if 'interval' not in kwargs:
        log.error('You must specifiy an interval')
        return False

    if 'target' not in kwargs:
        log.error('You must specifiy a target')
        return False

    if 'timeout' not in kwargs:
        log.error('You must specifiy a timeout')
        return False

    if 'unhealthythreshold' not in kwargs:
        log.error('You must specifiy an unhealthythreshold')
        return False

    if 'loadbalancername' not in kwargs:
        log.error('You must specifiy the name of the Elastic Load Balancer you want to configure')
        return False

    params = {'Action': 'ConfigureHealthCheck',
              'LoadBalancerName': kwargs['loadbalancername'],
              'HealthCheck.HealthyThreshold': kwargs['healthythreshold'],
              'HealthCheck.Interval': kwargs['interval'],
              'HealthCheck.Target': kwargs['target'],
              'HealthCheck.Timeout': kwargs['timeout'],
              'HealthCheck.UnhealthyThreshold': kwargs['unhealthythreshold']
              }

    data = query(params, return_root=True, endpoint_provider='elb')
    return data

def create_vpc(kwargs=None, call=None):
    '''
    Create a Virtual Private Cloud
    '''
    if call != 'function':
        log.error(
            'The create_vpc function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'cidr-block' not in kwargs:
        log.error('cidr-block must be specified.')
        return False

    if 'instance-tenancy' not in kwargs:
        kwargs['instance-tenancy'] = 'default'
        
    params = {'Action': 'CreateVpc',
              'InstanceTenancy': kwargs['instance-tenancy'],
              'CidrBlock': kwargs['cidr-block']
              }

    data = query(params, return_root=True)
    return data

def create_subnet(kwargs=None, call=None):
    '''
    Create a Subnet within a VPC
    '''
    if call != 'function':
        log.error(
            'The create_subnet function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'vpc-id' not in kwargs:
        log.error('vpc-id must be specified.')
        return False

    if 'cidr-block' not in kwargs:
        log.error('cidr-block must be specified.')
        return False

    params = {'Action': 'CreateSubnet',
              'VpcId': kwargs['vpc-id'],
              'CidrBlock': kwargs['cidr-block']
              }

    if 'zone' in kwargs:
        params['AvailabilityZone'] = kwargs['zone']

    data = query(params, return_root=True)
    return data

def create_igw(kwargs=None, call=None):
    '''
    Create an Internet Gateway for use with VPC
    '''
    if call != 'function':
        log.error(
            'The create_igw function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    params = {'Action': 'CreateInternetGateway'
              }

    data = query(params, return_root=True)
    return data
 
def attach_igw(kwargs=None, call=None):
    '''
    Attach an existing Internet Gateway to VPC
    '''
    if call != 'function':
        log.error(
            'The attach_igw function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'vpc-id' not in kwargs:
        log.error('vpc-id must be specified.')
        return False

    if 'igw-id' not in kwargs:
        log.error('igw-id must be specified.')
        return False

    params = {'Action': 'AttachInternetGateway',
              'VpcId': kwargs['vpc-id'],
              'InternetGatewayId': kwargs['igw-id']
              }

    data = query(params, return_root=True)
    return data

def create_routetable(kwargs=None, call=None):
    '''
    Create a Route Table within a VPC
    '''
    if call != 'function':
        log.error(
            'The create_routettable function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'vpc-id' not in kwargs:
        log.error('vpc-id must be specified.')
        return False

    params = {'Action': 'CreateRouteTable',
              'VpcId': kwargs['vpc-id']
              }

    data = query(params, return_root=True)
    return data

def create_route(kwargs=None, call=None):
    '''
    Create a route within an existing route table
    '''
    if call != 'function':
        log.error(
            'The create_route function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'rtb-id' not in kwargs:
        log.error('routetablrtb-id must be specified.')
        return False

    if 'dest-cidr-block' not in kwargs:
        log.error('dest-cidr-block must be specified.')
        return False

    params = {'Action': 'CreateRoute',
              'RouteTableId': kwargs['rtb-id'],
              'DestinationCidrBlock': kwargs['dest-cidr-block']
              }

    if 'gateway-id' in kwargs:
        params['GatewayId'] = kwargs['gateway-id']
    elif 'instance-id' in kwargs:
        params['InstanceId'] = kwargs['instance-id']
    elif 'interface-id' in kwargs:
        params['NetworkInterfaceId'] = kwargs['interface-id']
    else:
        log.error('One of gateway-id, instance-id, interface-id must be specified.')
        return False

    data = query(params, return_root=True)
    return data

def attach_subnet(kwargs=None, call=None):
    '''
    Associate an existing subnet with an existing route table
    '''
    if call != 'function':
        log.error(
            'The attach_subnet function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'rtb-id' not in kwargs:
        log.error('rtb-id must be specified.')
        return False

    if 'subnet-id' not in kwargs:
        log.error('subnet-id must be specified.')
        return False

    params = {'Action': 'AssociateRouteTable',
              'RouteTableId': kwargs['rtb-id'],
              'SubnetId': kwargs['subnet-id']
              }

    data = query(params, return_root=True)
    return data

def create_eip(kwargs=None, call=None):
    '''
    Allocate an Elastic IP Address
    '''
    if call != 'function':
        log.error(
            'The create_eip function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    params = {'Action': 'AllocateAddress',
              }

    if 'domain' in kwargs:
        params['Domain'] = kwargs['domain']

    data = query(params, return_root=True)
    return data

# TODO make into an actoin
def attach_eip(kwargs=None, call=None):
    '''
    Associate an Elastic IP Address with an existing instance
    '''
    if call != 'function':
        log.error(
            'The create_eip function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    params = {'Action': 'AssociateAddress' }
    
    if 'public-ip' in kwargs:
        params['PublicIp'] = kwargs['public-ip']

    if 'instance-id' in kwargs:
        params['InstanceId'] = kwargs['instance-id']

    if 'allocation-id' in kwargs:
        params['AllocationId'] = kwargs['allocation-id']

    if 'inteface-id' in kwargs:
        params['NetworkInterfaceId'] = kwargs['interface-id']

    if 'private-ip' in kwargs:
        params['PrivateIpAddress'] = kwargs['private-ip']

    if 'allow-reassociation' in kwargs:
        params['AllowReassociation'] = kwargs['allow-reassociation']

    data = query(params, return_root=True)
    return data

def create_sg(kwargs=None, call=None):
    '''
    Create a new Security Group
    '''
    if call != 'function':
        log.error(
            'The create_sg function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'group-name' not in kwargs:
        log.error('group-name must be specified.')
        return False

    if 'group-desc' not in kwargs:
        log.error('group-desc must be specified.')
        return False

    params = {'Action': 'CreateSecurityGroup',
              'GroupName': kwargs['group-name'],
              'GroupDescription': kwargs['group-desc'],
              }

    if 'vpc-id' in kwargs:
        params['VpcId'] = kwargs['vpc-id']
        
    data = query(params, return_root=True)
    return data


def create_ingress_rule(kwargs=None, call=None):
    '''
    Create a new Security Group Ingress Rule on an existing Security Group
    '''
    if call != 'function':
        log.error(
            'The create_ingress_rule function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    params = { 'Action': 'AuthorizeSecurityGroupIngress' }

    if 'group-id' in kwargs:
        params['GroupId'] = kwargs['group-id']
    elif 'group-name' in kwargs:
        params['GroupName'] = kwargs['group-name']
    else:
        log.error('One of group-name or group-id must be specified.')
        return False

    if not _parse_ip_permissions(kwargs, params):
        log.error('Failed to parse rules (IpPermissions)')
        return False
        
    data = query(params, return_root=True)
    return data

def create_egress_rule(kwargs=None, call=None):
    '''
    Create a new Security Group Egress Rule on an existing Security Group
    '''
    if call != 'function':
        log.error(
            'The create_egress_rule function must be called with -f or --function.'
        )
        return False

    if not kwargs:
        kwargs = {}

    if 'group-id' not in kwargs:
        log.error('group-id must be specified.')
        return False

    params = { 'Action': 'AuthorizeSecurityGroupEgress',
               'GroupId': kwargs['group-id'] }

    if not _parse_ip_permissions(kwargs, params):
        log.error('Failed to parse rules (IpPermissions)')
        return False

    data = query(params, return_root=True)
    return data

def _parse_ip_permissions(kwargs = None, params = None):
    if not isinstance(kwargs, dict) or not isinstance(params, dict):
        log.error('_parse_ip_permissions must be passed instances of kwargs and params')
        return False

    if 'rules' not in kwargs:
        log.error('rules is a required parameter')
        return False

    if not isinstance(kwargs['rules'], list):
        rules = _parse_str_parameters(kwargs['rules'])
    else:
        rules = kwargs['rules']
    for index in range(0, len(rules)):
        rule = rules[index]
        if 'protocol' in rule:
            params['IpPermissions.{0}.IpProtocol'.format(index+1)] = rule['protocol']
            if 'from-port' in rule:
                params['IpPermissions.{0}.FromPort'.format(index+1)] = rule['from-port']
            if 'to-port' in rule:
                params['IpPermissions.{0}.ToPort'.format(index+1)] = rule['to-port']
            # TODO: make this implementation support multiple ip-ranges / groups per rule
            if 'ip-range' in rule:
                params['IpPermissions.{0}.IpRanges.1.CidrIp'.format(index+1)] = rule['ip-range']                
            if 'group-name' in rule:
                params['IpPermissions.{0}.Groups.1.GroupName'.format(index+1)] = rule['group-name']                
            if 'group-id' in rule:
                params['IpPermissions.{0}.Groups.1.GroupId'.format(index+1)] = rule['group-id']              
            if 'user-id' in rule:
                params['IpPermissions.{0}.Groups.1.UserId'.format(index+1)] = rule['user-id']                
        else:
            log.error('rules.protocol is a required parameter')
            return False

    return True

def _parse_str_parameters(params):
    # Parses strings in the format 'a=1,b=2;c=3,d=4' in to a list of dictionaries
    # i.e. [{'a': '1', 'b': '2'}, {'a': '3', 'b': '4'}]
    return [dict(val.split('=') for val in param.split(',')) for param in params.split(';')]
