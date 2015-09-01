# ----------------------------------------------------------------------
#    Copyright (C) 2013 Kshitij Gupta <kgupta8592@gmail.com>
#
#    This program is free software; you can redistribute it and/or
#    modify it under the terms of version 2 of the GNU General Public
#    License as published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
# ----------------------------------------------------------------------
import os
import re
import sys
import time
import LibAppArmor
from apparmor.common import AppArmorException, open_file_read, DebugLogger

from apparmor.aamode import validate_log_mode, log_str_to_mode, hide_log_mode, AA_MAY_EXEC

# setup module translations
from apparmor.translations import init_translation
_ = init_translation()

class ReadLog:
    RE_LOG_v2_6_syslog = re.compile('kernel:\s+(\[[\d\.\s]+\]\s+)?type=\d+\s+audit\([\d\.\:]+\):\s+apparmor=')
    RE_LOG_v2_6_audit = re.compile('type=AVC\s+(msg=)?audit\([\d\.\:]+\):\s+apparmor=')
    # Used by netdomain to identify the operation types
    # New socket names
    OPERATION_TYPES = {'create': 'net',
                       'post_create': 'net',
                       'bind': 'net',
                       'connect': 'net',
                       'listen': 'net',
                       'accept': 'net',
                       'sendmsg': 'net',
                       'recvmsg': 'net',
                       'getsockname': 'net',
                       'getpeername': 'net',
                       'getsockopt': 'net',
                       'setsockopt': 'net',
                       'sock_shutdown': 'net'
                       }

    def __init__(self, pid, filename, existing_profiles, profile_dir, log):
        self.filename = filename
        self.profile_dir = profile_dir
        self.pid = pid
        self.existing_profiles = existing_profiles
        self.log = log
        self.debug_logger = DebugLogger('ReadLog')
        self.LOG = None
        self.logmark = ''
        self.seenmark = None
        self.next_log_entry = None

    def prefetch_next_log_entry(self):
        if self.next_log_entry:
            sys.stderr.out('A log entry already present: %s' % self.next_log_entry)
        self.next_log_entry = self.LOG.readline()
        while not self.RE_LOG_v2_6_syslog.search(self.next_log_entry) and not self.RE_LOG_v2_6_audit.search(self.next_log_entry) and not (self.logmark and self.logmark in self.next_log_entry):
            self.next_log_entry = self.LOG.readline()
            if not self.next_log_entry:
                break

    def get_next_log_entry(self):
        # If no next log entry fetch it
        if not self.next_log_entry:
            self.prefetch_next_log_entry()
        log_entry = self.next_log_entry
        self.next_log_entry = None
        return log_entry

    def peek_at_next_log_entry(self):
        # Take a peek at the next log entry
        if not self.next_log_entry:
            self.prefetch_next_log_entry()
        return self.next_log_entry

    def throw_away_next_log_entry(self):
        self.next_log_entry = None

    def parse_log_record(self, record):
        self.debug_logger.debug('parse_log_record: %s' % record)

        record_event = self.parse_event(record)
        return record_event

    def parse_event(self, msg):
        """Parse the event from log into key value pairs"""
        msg = msg.strip()
        self.debug_logger.info('parse_event: %s' % msg)
        #print(repr(msg))
        if sys.version_info < (3, 0):
            # parse_record fails with u'foo' style strings hence typecasting to string
            msg = str(msg)
        event = LibAppArmor.parse_record(msg)
        ev = dict()
        ev['resource'] = event.info
        ev['active_hat'] = event.active_hat
        ev['aamode'] = event.event
        ev['time'] = event.epoch
        ev['operation'] = event.operation
        ev['profile'] = event.profile
        ev['name'] = event.name
        ev['name2'] = event.name2
        ev['attr'] = event.attribute
        ev['parent'] = event.parent
        ev['pid'] = event.pid
        ev['task'] = event.task
        ev['info'] = event.info
        dmask = event.denied_mask
        rmask = event.requested_mask
        ev['magic_token'] = event.magic_token
        if ev['operation'] and self.op_type(ev['operation']) == 'net':
            ev['family'] = event.net_family
            ev['protocol'] = event.net_protocol
            ev['sock_type'] = event.net_sock_type
        LibAppArmor.free_record(event)
        # Map c (create) to a and d (delete) to w, logprof doesn't support c and d
        if rmask:
            rmask = rmask.replace('c', 'a')
            rmask = rmask.replace('d', 'w')
            if not validate_log_mode(hide_log_mode(rmask)):
                raise AppArmorException(_('Log contains unknown mode %s') % rmask)
        if dmask:
            dmask = dmask.replace('c', 'a')
            dmask = dmask.replace('d', 'w')
            if not validate_log_mode(hide_log_mode(dmask)):
                raise AppArmorException(_('Log contains unknown mode %s') % dmask)
        #print('parse_event:', ev['profile'], dmask, ev['name2'])
        mask, name = log_str_to_mode(ev['profile'], dmask, ev['name2'])

        ev['denied_mask'] = mask
        ev['name2'] = name

        mask, name = log_str_to_mode(ev['profile'], rmask, ev['name2'])
        ev['request_mask'] = mask
        ev['name2'] = name

        if not ev['time']:
            ev['time'] = int(time.time())
        # Remove None keys
        #for key in ev.keys():
        #    if not ev[key] or not re.search('[\w]+', ev[key]):
        #        ev.pop(key)

        if ev['aamode']:
            # Convert aamode values to their counter-parts
            mode_convertor = {0: 'UNKNOWN',
                              1: 'ERROR',
                              2: 'AUDIT',
                              3: 'PERMITTING',
                              4: 'REJECTING',
                              5: 'HINT',
                              6: 'STATUS'
                              }
            try:
                ev['aamode'] = mode_convertor[ev['aamode']]
            except KeyError:
                ev['aamode'] = None

        if ev['aamode']:
            #debug_logger.debug(ev)
            return ev
        else:
            return None

    def add_to_tree(self, loc_pid, parent, type, event):
        self.debug_logger.info('add_to_tree: pid [%s] type [%s] event [%s]' % (loc_pid, type, event))
        if not self.pid.get(loc_pid, False):
            profile, hat = event[:2]
            if parent and self.pid.get(parent, False):
                if not hat:
                    hat = 'null-complain-profile'
                arrayref = []
                self.pid[parent].append(arrayref)
                self.pid[loc_pid] = arrayref
                for ia in ['fork', loc_pid, profile, hat]:
                    arrayref.append(ia)
#                 self.pid[parent].append(array_ref)
#                 self.pid[loc_pid] = array_ref
            else:
                arrayref = []
                self.log.append(arrayref)
                self.pid[loc_pid] = arrayref
#                 self.log.append(array_ref)
#                 self.pid[loc_pid] = array_ref
        self.pid[loc_pid].append([type, loc_pid] + event)
        #print("\n\npid",self.pid)
        #print("log",self.log)

    def add_event_to_tree(self, e):
        aamode = e.get('aamode', 'UNKNOWN')
        if e.get('type', False):
            if re.search('(UNKNOWN\[1501\]|APPARMOR_AUDIT|1501)', e['type']):
                aamode = 'AUDIT'
            elif re.search('(UNKNOWN\[1502\]|APPARMOR_ALLOWED|1502)', e['type']):
                aamode = 'PERMITTING'
            elif re.search('(UNKNOWN\[1503\]|APPARMOR_DENIED|1503)', e['type']):
                aamode = 'REJECTING'
            elif re.search('(UNKNOWN\[1504\]|APPARMOR_HINT|1504)', e['type']):
                aamode = 'HINT'
            elif re.search('(UNKNOWN\[1505\]|APPARMOR_STATUS|1505)', e['type']):
                aamode = 'STATUS'
            elif re.search('(UNKNOWN\[1506\]|APPARMOR_ERROR|1506)', e['type']):
                aamode = 'ERROR'
            else:
                aamode = 'UNKNOWN'

        if aamode in ['UNKNOWN', 'AUDIT', 'STATUS', 'ERROR']:
            return None

        if 'profile_set' in e['operation']:
            return None

        # Skip if AUDIT event was issued due to a change_hat in unconfined mode
        if not e.get('profile', False):
            return None

        # Convert new null profiles to old single level null profile
        if '//null-' in e['profile']:
            e['profile'] = 'null-complain-profile'

        profile = e['profile']
        hat = None

        if '//' in e['profile']:
            profile, hat = e['profile'].split('//')[:2]

        # Filter out change_hat events that aren't from learning
        if e['operation'] == 'change_hat':
            if aamode != 'HINT' and aamode != 'PERMITTING':
                return None
            profile = e['name']
            #hat = None
            if '//' in e['name']:
                profile, hat = e['name'].split('//')[:2]

        if not hat:
            hat = profile

        # prog is no longer passed around consistently
        prog = 'HINT'

        if profile != 'null-complain-profile' and not self.profile_exists(profile):
            return None
        if e['operation'] == 'exec':
            if e.get('info', False) and e['info'] == 'mandatory profile missing':
                self.add_to_tree(e['pid'], e['parent'], 'exec',
                                 [profile, hat, aamode, 'PERMITTING', e['denied_mask'], e['name'], e['name2']])
            elif (e.get('name2', False) and '//null-' in e['name2']) or e.get('name', False):
                self.add_to_tree(e['pid'], e['parent'], 'exec',
                                 [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])
            else:
                self.debug_logger.debug('add_event_to_tree: dropped exec event in %s' % e['profile'])

        elif 'file_' in e['operation']:
            self.add_to_tree(e['pid'], e['parent'], 'path',
                             [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])
        elif e['operation'] in ['open', 'truncate', 'mkdir', 'mknod', 'rename_src',
                                'rename_dest', 'unlink', 'rmdir', 'symlink_create', 'link']:
            #print(e['operation'], e['name'])
            self.add_to_tree(e['pid'], e['parent'], 'path',
                             [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])
        elif e['operation'] == 'capable':
            self.add_to_tree(e['pid'], e['parent'], 'capability',
                             [profile, hat, prog, aamode, e['name'], ''])
        elif e['operation'] == 'setattr' or 'xattr' in e['operation']:
            self.add_to_tree(e['pid'], e['parent'], 'path',
                             [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])
        elif 'inode_' in e['operation']:
            is_domain_change = False
            if e['operation'] == 'inode_permission' and (e['denied_mask'] & AA_MAY_EXEC) and aamode == 'PERMITTING':
                following = self.peek_at_next_log_entry()
                if following:
                    entry = self.parse_log_record(following)
                    if entry and entry.get('info', False) == 'set profile':
                        is_domain_change = True
                        self.throw_away_next_log_entry()

            if is_domain_change:
                self.add_to_tree(e['pid'], e['parent'], 'exec',
                                 [profile, hat, prog, aamode, e['denied_mask'], e['name'], e['name2']])
            else:
                self.add_to_tree(e['pid'], e['parent'], 'path',
                                 [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])

        elif e['operation'] == 'sysctl':
            self.add_to_tree(e['pid'], e['parent'], 'path',
                             [profile, hat, prog, aamode, e['denied_mask'], e['name'], ''])

        elif e['operation'] == 'clone':
            parent, child = e['pid'], e['task']
            if not parent:
                parent = 'null-complain-profile'
            if not hat:
                hat = 'null-complain-profile'
            arrayref = []
            if self.pid.get(parent, False):
                self.pid[parent].append(arrayref)
            else:
                self.log.append(arrayref)
            self.pid[child].append(arrayref)
            for ia in ['fork', child, profile, hat]:
                arrayref.append(ia)
#             if self.pid.get(parent, False):
#                 self.pid[parent] += [arrayref]
#             else:
#                 self.log += [arrayref]
#             self.pid[child] = arrayref

        elif self.op_type(e['operation']) == 'net':
            self.add_to_tree(e['pid'], e['parent'], 'netdomain',
                             [profile, hat, prog, aamode, e['family'], e['sock_type'], e['protocol']])
        elif e['operation'] == 'change_hat':
            self.add_to_tree(e['pid'], e['parent'], 'unknown_hat',
                             [profile, hat, aamode, hat])
        else:
            self.debug_logger.debug('UNHANDLED: %s' % e)

    def read_log(self, logmark):
        self.logmark = logmark
        seenmark = True
        if self.logmark:
            seenmark = False
        #last = None
        #event_type = None
        try:
            #print(self.filename)
            self.LOG = open_file_read(self.filename)
        except IOError:
            raise AppArmorException('Can not read AppArmor logfile: ' + self.filename)
        #LOG = open_file_read(log_open)
        line = True
        while line:
            line = self.get_next_log_entry()
            if not line:
                break
            line = line.strip()
            self.debug_logger.debug('read_log: %s' % line)
            if self.logmark in line:
                seenmark = True

            self.debug_logger.debug('read_log: seenmark = %s' % seenmark)
            if not seenmark:
                continue

            event = self.parse_log_record(line)
            #print(event)
            if event:
                self.add_event_to_tree(event)
        self.LOG.close()
        self.logmark = ''
        return self.log

    def op_type(self, operation):
        """Returns the operation type if known, unkown otherwise"""
        operation_type = self.OPERATION_TYPES.get(operation, 'unknown')
        return operation_type

    def profile_exists(self, program):
        """Returns True if profile exists, False otherwise"""
        # Check cache of profiles
        if self.existing_profiles.get(program, False):
            return True
        # Check the disk for profile
        prof_path = self.get_profile_filename(program)
        #print(prof_path)
        if os.path.isfile(prof_path):
            # Add to cache of profile
            self.existing_profiles[program] = prof_path
            return True
        return False

    def get_profile_filename(self, profile):
        """Returns the full profile name"""
        if profile.startswith('/'):
            # Remove leading /
            profile = profile[1:]
        else:
            profile = "profile_" + profile
        profile = profile.replace('/', '.')
        full_profilename = self.profile_dir + '/' + profile
        return full_profilename
