#!/usr/bin/env python3
# -*- coding: utf8 -*-
#
# Amazon Web Services CLI - LastPass SAML integration
#
# This script uses LastPass Enterprise SAML-based login to authenticate
# with AWS and retrieve a session token that can then be used with the
# AWS cli tool.
#
# Copyright (c) 2016 LastPass
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
import sys
import re
import requests
import hmac
import hashlib
import binascii
import logging
import xml.etree.ElementTree as ET
from base64 import b64decode, b64encode
from struct import pack
import os
import argparse

import boto3
from six.moves import input
from six.moves import html_parser
from six.moves import configparser

from getpass import getpass
from lastpass import fetcher


LASTPASS_SERVER = 'https://lastpass.com'

# for debugging with proxy
PROXY_SERVER = 'https://127.0.0.1:8443'
# LASTPASS_SERVER = PROXY_SERVER

logging.basicConfig(level=logging.CRITICAL)
logger = logging.getLogger('lp-aws-saml')

class MfaRequiredException(Exception):
    pass

def should_verify():
    """ Disable SSL validation only when debugging via proxy """
    return LASTPASS_SERVER != PROXY_SERVER


def extract_form(html):
    """
    Retrieve the (first) form elements from an html page.
    """
    fields = {}
    matches = re.findall(r'name="([^"]*)" (id="([^"]*)" )?value="([^"]*)"',
                         html)
    for match in matches:
        if len(match) > 2:
            fields[match[0]] = match[3]

    action = ''
    match = re.search(r'action="([^"]*)"', html)
    if match:
        action = match.group(1)

    form = {
        'action': action,
        'fields': fields
    }
    return form



def get_saml_token(session, username, password, saml_cfg_id):
    """
    Log into LastPass and retrieve a SAML token for a given
    SAML configuration.
    """
    logger.debug("Getting SAML token")

    # now logged in, grab the SAML token from the IdP-initiated login
    idp_login = '%s/saml/launch/cfg/%d' % (LASTPASS_SERVER, saml_cfg_id)

    r = requests.get(idp_login,cookies={'PHPSESSID': session.id}, verify=should_verify())

    form = extract_form(r.text)
    if not form['action']:
        # try to scrape the error message just to make it more user friendly
        error = ""
        for l in r.text.splitlines():
            match = re.search(r'<h2>(.*)</h2>', l)
            if match:
                msg = html_parser.HTMLParser().unescape(match.group(1))
                msg = msg.replace("<br/>", "\n")
                msg = msg.replace("<b>", "")
                msg = msg.replace("</b>", "")
                error = "\n" + msg

        raise ValueError("Unable to find SAML ACS" + error)

    return b64decode(form['fields']['SAMLResponse'])


def get_saml_aws_roles(assertion):
    """
    Get the AWS roles contained in the assertion.  This returns a list of
    RoleARN, PrincipalARN (IdP) pairs.
    """
    doc = ET.fromstring(assertion)

    role_attrib = 'https://aws.amazon.com/SAML/Attributes/Role'
    xpath = ".//saml:Attribute[@Name='%s']/saml:AttributeValue" % role_attrib
    ns = {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'}

    attribs = doc.findall(xpath, ns)
    return [a.text.split(",", 2) for a in attribs]


def get_saml_nameid(assertion):
    """
    Get the AWS roles contained in the assertion.  This returns a list of
    RoleARN, PrincipalARN (IdP) pairs.
    """
    doc = ET.fromstring(assertion)

    ns = {'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'}
    return doc.find(".//saml:NameID", ns).text


def prompt_for_role(roles):
    """
    Ask user which role to assume.
    """
    if len(roles) == 1:
        return roles[0]

    print ('Please select a role:')
    count = 1
    for r in roles:
        print ('  %d) %s' % (count, r[0]))
        count = count + 1

    choice = 0
    while choice < 1 or choice > len(roles) + 1:
        try:
            choice = int(input("Choice: "))
        except ValueError:
            choice = 0

    return roles[choice - 1]


def aws_assume_role(assertion, role_arn, principal_arn):
    client = boto3.client('sts',
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_session_token="",
    )
    short_creds = client.assume_role_with_saml(
                RoleArn=role_arn,
                PrincipalArn=principal_arn,
                SAMLAssertion=b64encode(assertion).decode("utf-8"),
                DurationSeconds=900)
    credentials = short_creds['Credentials']
    role_name = role_arn.rsplit('/', 1)[1]
    iam = boto3.resource(
        'iam',
        aws_access_key_id=credentials['AccessKeyId'],
        aws_secret_access_key=credentials['SecretAccessKey'],
        aws_session_token=credentials['SessionToken'],
    )
    try:
        duration = iam.Role(role_name).max_session_duration
    except:
        return [short_creds,900]

    return [client.assume_role_with_saml(
                RoleArn=role_arn,
                PrincipalArn=principal_arn,
                SAMLAssertion=b64encode(assertion).decode("utf-8"),
                DurationSeconds=duration) , duration ]


def aws_set_profile(profile_name, response):
    """
    Save AWS credentials returned from Assume Role operation in
    ~/.aws/credentials INI file.  The credentials are saved in
    a profile with [profile_name].
    """
    config_fn = os.path.expanduser("~/.aws/credentials")

    config = configparser.ConfigParser()
    config.read(config_fn)

    section = profile_name
    try:
        config.add_section(section)
    except configparser.DuplicateSectionError:
        pass

    try:
        os.makedirs(os.path.dirname(config_fn))
    except OSError:
        pass

    config.set(section, 'aws_access_key_id',
               response['Credentials']['AccessKeyId'])
    config.set(section, 'aws_secret_access_key',
               response['Credentials']['SecretAccessKey'])
    config.set(section, 'aws_session_token',
               response['Credentials']['SessionToken'])
    with open(config_fn, 'w') as out:
        config.write(out)


def main():
    parser = argparse.ArgumentParser(description='Get temporary AWS access credentials using LastPass SAML Login')
    parser.add_argument('username', type=str,
                    help='the lastpass username')
    parser.add_argument('saml_config_id', type=int,
                    help='the lastpass SAML config id')
    parser.add_argument('--profile-name', dest='profile_name',
                    help='the name of AWS profile to save the data in (default username)')

    args = parser.parse_args()

    username = args.username
    saml_cfg_id = args.saml_config_id

    if args.profile_name is not None:
        profile_name = args.profile_name
    else:
        profile_name = username

    password = getpass()

    session = requests.Session()

    try:
      session = fetcher.login(username, password)
    except:
      otp = input("OTP: ")
      session = fetcher.login(username, password, otp)

    assertion = get_saml_token(session, username, password, saml_cfg_id)
    roles = get_saml_aws_roles(assertion)
    user = get_saml_nameid(assertion)

    role = prompt_for_role(roles)
    response = aws_assume_role(assertion, role[0], role[1])
    aws_set_profile(profile_name, response[0])

    print ("A new AWS CLI profile '%s' has been added." % profile_name)
    print ("You may now invoke the aws CLI tool as follows:")
    print
    print ("    aws --profile %s [...] " % profile_name)
    print
    print ("This profile is valid for %ds" % response[1] )


if __name__ == "__main__":
    main()
