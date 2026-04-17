#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Advanced Bulk Email Sender - Complete Working Version (Optimized)
Admin Controlled Access - Codes for 10 users each
With Comprehensive Error Reporting to Telegram
"""

import os
import time
import logging
import threading
import smtplib
import random
import re
import secrets
import string
import socket
import json
import asyncio
from functools import lru_cache, wraps
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import concurrent.futures

# Telegram imports
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# ===================== CONFIGURATION =====================

TELEGRAM_BOT_TOKEN = "7602491205:AAGZ2XXo6gMyPXc4HRS5CdzGRW2FUhZW03o"
ADMIN_USER_ID = 6545531237
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_DELAY = 10
MAX_USERS_PER_CODE = 10
# Performance optimization constants
BATCH_SIZE = 50  # Process emails in batches for better memory usage
CONNECTION_POOL_SIZE = 3  # Reuse SMTP connections
CACHE_SIZE = 128  # LRU cache size for repeated operations

# ===================== DATA STRUCTURES =====================

@dataclass
class EmailAccount:
    email: str
    password: str
    name: str = ""
    is_default: bool = False
    last_auth_failure: str = ""
    auth_failure_count: int = 0
    connection_failure_count: int = 0
    last_connection_failure: str = ""
    
    def __post_init__(self):
        self.last_auth_failure = self.last_auth_failure or ""
        self.auth_failure_count = self.auth_failure_count or 0
        self.connection_failure_count = self.connection_failure_count or 0
        self.last_connection_failure = self.last_connection_failure or ""

@dataclass
class AccessCode:
    code: str
    created_by: int
    created_at: str
    max_users: int = MAX_USERS_PER_CODE
    users_used: List[int] = field(default_factory=list)
    is_active: bool = True
    
    def can_use(self, user_id: int) -> bool:
        return self.is_active and len(self.users_used) < self.max_users and user_id not in self.users_used
    
    def use(self, user_id: int) -> bool:
        if self.can_use(user_id):
            self.users_used.append(user_id)
            return True
        return False
    
    def get_usage_count(self) -> int:
        return len(self.users_used)
    
    def get_remaining_uses(self) -> int:
        return self.max_users - len(self.users_used)

@dataclass
class EmailError:
    recipient: str
    error_type: str
    error_message: str
    timestamp: str
    attempts: int = 1

@dataclass
class BulkEmailJob:
    user_id: int
    sender: EmailAccount
    recipient_list: List[str]
    subject: str
    body: str
    is_html: bool = False
    current_index: int = 0
    sent: int = 0
    failed: int = 0
    is_running: bool = False
    is_paused: bool = False
    should_stop: bool = False
    thread: threading.Thread = None
    start_time: float = field(default_factory=time.time)
    errors: List[EmailError] = field(default_factory=list)
    error_summary: Dict[str, int] = field(default_factory=lambda: {
        "authentication": 0,
        "connection": 0,
        "smtp": 0,
        "general": 0
    })

# ===================== CACHE DECORATOR =====================

def cached(ttl_seconds=300):
    """Simple cache decorator with TTL"""
    cache = {}
    
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = time.time()
            
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < ttl_seconds:
                    return result
            
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
        return wrapper
    return decorator

# ===================== STORAGE SERVICE (OPTIMIZED) =====================

class StorageService:
    def __init__(self):
        self.emails_file = "emails.json"
        self.access_file = "access_data.json"
        self.access_codes = {}
        self.authorized_users = {}
        self._cache = {}  # In-memory cache for frequently accessed data
        self._cache_timestamps = {}
        self._lock = threading.RLock()  # Thread-safe operations
        self.load_access_data()
        
        if ADMIN_USER_ID != 0 and str(ADMIN_USER_ID) not in self.authorized_users:
            self.authorized_users[str(ADMIN_USER_ID)] = {
                'authorized_by': 'SYSTEM',
                'authorized_at': datetime.now().isoformat(),
                'username': 'Admin'
            }
            self.save_access_data()
        
        self.check_specific_codes()
    
    def _get_cached(self, key: str, max_age: int = 60) -> Optional[any]:
        """Get cached data if not expired"""
        if key in self._cache:
            if time.time() - self._cache_timestamps.get(key, 0) < max_age:
                return self._cache[key]
        return None
    
    def _set_cached(self, key: str, value: any):
        """Set cached data"""
        self._cache[key] = value
        self._cache_timestamps[key] = time.time()
    
    def _invalidate_cache(self, pattern: str = None):
        """Invalidate cache entries"""
        if pattern:
            keys_to_remove = [k for k in self._cache.keys() if pattern in k]
            for k in keys_to_remove:
                del self._cache[k]
                del self._cache_timestamps[k]
        else:
            self._cache.clear()
            self._cache_timestamps.clear()
    
    def check_specific_codes(self):
        specific_codes = [{
            'code': 'OWK475',
            'created_by': ADMIN_USER_ID,
            'created_at': datetime.now().isoformat(),
            'max_users': MAX_USERS_PER_CODE,
            'users_used': [],
            'is_active': False,
            'notes': 'No longer working'
        }]
        
        updated = False
        for spec_code in specific_codes:
            code = spec_code['code']
            if code not in self.access_codes:
                self.access_codes[code] = spec_code
                updated = True
                logging.info(f"📝 Added specific code: {code}")
        
        if updated:
            self.save_access_data()
    
    def load_access_data(self):
        with self._lock:
            if os.path.exists(self.access_file):
                try:
                    with open(self.access_file, 'r') as f:
                        data = json.load(f)
                        self.authorized_users = data.get('authorized_users', {})
                        self.access_codes = data.get('access_codes', {})
                except:
                    self.authorized_users = {}
                    self.access_codes = {}
    
    def save_access_data(self):
        with self._lock:
            try:
                data = {
                    'authorized_users': self.authorized_users,
                    'access_codes': self.access_codes
                }
                with open(self.access_file, 'w') as f:
                    json.dump(data, f, indent=2)
                self._invalidate_cache()
                return True
            except:
                return False
    
    @cached(ttl_seconds=30)
    def is_admin(self, user_id: int) -> bool:
        return user_id == ADMIN_USER_ID
    
    @cached(ttl_seconds=30)
    def is_authorized(self, user_id: int) -> bool:
        return str(user_id) in self.authorized_users or self.is_admin(user_id)
    
    def verify_access_code(self, code: str, user_id: int) -> tuple:
        if self.is_admin(user_id):
            return True, "👑 You are already admin!"
        
        code = code.upper().strip()
        
        if code not in self.access_codes:
            return False, "❌ Invalid access code"
        
        code_data = self.access_codes[code]
        users_used = code_data.get('users_used', [])
        max_users = code_data.get('max_users', MAX_USERS_PER_CODE)
        is_active = code_data.get('is_active', True)
        notes = code_data.get('notes', '')
        
        if not is_active:
            status_msg = "❌ This code has been deactivated"
            if notes:
                status_msg += f"\n📝 Note: {notes}"
            return False, status_msg
        
        if user_id in users_used:
            return False, "❌ You have already used this code"
        
        if len(users_used) >= max_users:
            return False, "❌ This code has reached its limit"
        
        users_used.append(user_id)
        self.access_codes[code]['users_used'] = users_used
        self.authorized_users[str(user_id)] = {
            'authorized_by': code,
            'authorized_at': datetime.now().isoformat(),
            'username': ''
        }
        
        self.save_access_data()
        return True, "✅ Access granted! Welcome!"
    
    def generate_access_code(self, admin_id: int) -> str:
        if not self.is_admin(admin_id):
            return ""
        
        letters = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(3))
        numbers = ''.join(secrets.choice(string.digits) for _ in range(3))
        code = f"{letters}{numbers}"
        
        self.access_codes[code] = {
            'code': code,
            'created_by': admin_id,
            'created_at': datetime.now().isoformat(),
            'max_users': MAX_USERS_PER_CODE,
            'users_used': [],
            'is_active': True,
            'notes': ''
        }
        
        self.save_access_data()
        return code
    
    def list_codes(self, admin_id: int) -> str:
        if not self.is_admin(admin_id):
            return "❌ Admin only"
        
        if not self.access_codes:
            return "📋 No access codes generated yet."
        
        result = "📋 ACCESS CODES:\n\n"
        for code, data in self.access_codes.items():
            status = '🟢 ACTIVE' if data.get('is_active', True) else '🔴 INACTIVE'
            users_used = len(data.get('users_used', []))
            max_users = data.get('max_users', MAX_USERS_PER_CODE)
            notes = data.get('notes', '')
            
            result += f"🔑 Code: {code}\n"
            result += f"   Status: {status}\n"
            result += f"   Created: {data.get('created_at', 'Unknown')[:10]}\n"
            result += f"   Usage: {users_used}/{max_users} users\n"
            result += f"   Remaining: {max_users - users_used} users\n"
            if notes:
                result += f"   📝 Notes: {notes[:50]}...\n" if len(notes) > 50 else f"   📝 Notes: {notes}\n"
            
            if users_used > 0:
                result += f"   👥 Users (first 5): "
                user_ids = data.get('users_used', [])[:5]
                result += f"{', '.join([str(uid) for uid in user_ids])}"
                if users_used > 5:
                    result += f" +{users_used - 5} more"
                result += "\n"
            
            result += "─" * 40 + "\n"
        
        return result
    
    def get_code_info(self, code: str) -> Dict:
        code = code.upper().strip()
        return self.access_codes.get(code)
    
    def check_code_status(self, code: str) -> str:
        code = code.upper().strip()
        if code not in self.access_codes:
            return f"❌ Code {code} not found in database"
        
        data = self.access_codes[code]
        users_used = len(data.get('users_used', []))
        max_users = data.get('max_users', MAX_USERS_PER_CODE)
        is_active = data.get('is_active', True)
        created_at = data.get('created_at', 'Unknown')
        created_by = data.get('created_by', 'Unknown')
        notes = data.get('notes', '')
        
        result = f"🔍 CODE DETAILS: {code}\n\n"
        result += f"📊 Status: {'🟢 ACTIVE' if is_active else '🔴 INACTIVE'}\n"
        result += f"👤 Created by: {created_by}\n"
        result += f"📅 Created: {created_at[:10] if len(created_at) > 10 else created_at}\n"
        result += f"👥 Users used: {users_used}/{max_users}\n"
        result += f"📈 Remaining uses: {max_users - users_used}\n"
        
        if notes:
            result += f"📝 Notes: {notes}\n"
        
        if users_used > 0:
            result += f"\n📋 USERS WHO USED THIS CODE:\n"
            user_list = data.get('users_used', [])
            for i, user_id in enumerate(user_list[:10], 1):
                user_info = self.authorized_users.get(str(user_id), {})
                username = user_info.get('username', 'Unknown')
                authorized_at = user_info.get('authorized_at', 'Unknown')
                
                result += f"{i}. ID: {user_id}\n"
                result += f"   👤 Username: {username}\n"
                result += f"   📅 Authorized: {authorized_at[:10] if len(authorized_at) > 10 else authorized_at}\n"
            
            if len(user_list) > 10:
                result += f"\n... and {len(user_list) - 10} more users\n"
        else:
            result += f"\n👤 No users have used this code yet.\n"
        
        return result
    
    def toggle_code_status(self, code: str, admin_id: int) -> str:
        if not self.is_admin(admin_id):
            return "❌ Admin only command"
        
        code = code.upper().strip()
        if code not in self.access_codes:
            return f"❌ Code {code} not found"
        
        current_status = self.access_codes[code].get('is_active', True)
        self.access_codes[code]['is_active'] = not current_status
        new_status = "🟢 ACTIVATED" if not current_status else "🔴 DEACTIVATED"
        
        if 'notes' not in self.access_codes[code]:
            self.access_codes[code]['notes'] = ''
        
        self.access_codes[code]['notes'] += f" Status changed to {new_status.lower()} on {datetime.now().strftime('%Y-%m-%d')}. "
        
        self.save_access_data()
        return f"✅ Code {code} has been {new_status.lower()}"
    
    def add_code_note(self, code: str, note: str, admin_id: int) -> str:
        if not self.is_admin(admin_id):
            return "❌ Admin only command"
        
        code = code.upper().strip()
        if code not in self.access_codes:
            return f"❌ Code {code} not found"
        
        if 'notes' not in self.access_codes[code]:
            self.access_codes[code]['notes'] = ''
        
        self.access_codes[code]['notes'] += f"\n📝 {datetime.now().strftime('%Y-%m-%d')}: {note}"
        self.save_access_data()
        return f"✅ Note added to code {code}"
    
    def get_active_codes(self) -> List[str]:
        return [code for code, data in self.access_codes.items() if data.get('is_active', True)]
    
    def get_inactive_codes(self) -> List[str]:
        return [code for code, data in self.access_codes.items() if not data.get('is_active', True)]
    
    def get_active_users(self) -> Dict:
        return {uid: data for uid, data in self.authorized_users.items() if int(uid) != ADMIN_USER_ID}
    
    def activate_code_with_users(self, code: str, users_count: int = 0, admin_id: int = None) -> str:
        if admin_id and not self.is_admin(admin_id):
            return "❌ Admin only command"
        
        code = code.upper().strip()
        if code not in self.access_codes:
            return f"❌ Code {code} not found"
        
        self.access_codes[code]['is_active'] = True
        
        if users_count > 0:
            current_users = len(self.access_codes[code].get('users_used', []))
            max_users = self.access_codes[code].get('max_users', MAX_USERS_PER_CODE)
            
            if current_users + users_count > max_users:
                return f"❌ Cannot add {users_count} users. Code already has {current_users} users, max is {max_users}"
            
            if 'users_used' not in self.access_codes[code]:
                self.access_codes[code]['users_used'] = []
            
            for i in range(users_count):
                dummy_id = -1000 - i - len(self.access_codes[code]['users_used'])
                self.access_codes[code]['users_used'].append(dummy_id)
            
            if 'notes' not in self.access_codes[code]:
                self.access_codes[code]['notes'] = ''
            
            self.access_codes[code]['notes'] += f" Activated on {datetime.now().strftime('%Y-%m-%d')} with {users_count} existing users. "
        
        self.save_access_data()
        
        users_used = len(self.access_codes[code].get('users_used', []))
        max_users = self.access_codes[code].get('max_users', MAX_USERS_PER_CODE)
        remaining = max_users - users_used
        
        message = f"✅ Code {code} activated!"
        if users_count > 0:
            message += f"\n📊 Added {users_count} existing users"
        message += f"\n👥 Current usage: {users_used}/{max_users} users"
        message += f"\n📈 Remaining uses: {remaining}"
        
        return message
    
    def list_users(self, admin_id: int) -> str:
        if not self.is_admin(admin_id):
            return "❌ Admin only"
        
        if not self.authorized_users:
            return "👤 No authorized users yet."
        
        result = "👤 AUTHORIZED USERS:\n\n"
        for user_id, data in list(self.authorized_users.items())[:20]:
            if int(user_id) == ADMIN_USER_ID:
                continue
            result += f"ID: {user_id}\n"
            result += f"   Code used: {data.get('authorized_by', 'Unknown')}\n"
            result += f"   Authorized: {data.get('authorized_at', 'Unknown')[:10]}\n"
            result += "─" * 30 + "\n"
        
        if len(self.authorized_users) > 20:
            result += f"\n... and {len(self.authorized_users) - 20} more users"
        
        return result
    
    def save_email(self, user_id: int, account: EmailAccount) -> bool:
        with self._lock:
            try:
                data = {}
                if os.path.exists(self.emails_file):
                    with open(self.emails_file, 'r') as f:
                        data = json.load(f)
                
                user_str = str(user_id)
                if user_str not in data:
                    data[user_str] = []
                
                data[user_str] = [acc for acc in data[user_str] if acc['email'] != account.email]
                
                acc_data = {
                    'email': account.email,
                    'password': account.password,
                    'name': account.name,
                    'is_default': account.is_default,
                    'last_auth_failure': account.last_auth_failure,
                    'auth_failure_count': account.auth_failure_count,
                    'connection_failure_count': account.connection_failure_count,
                    'last_connection_failure': account.last_connection_failure
                }
                data[user_str].append(acc_data)
                
                with open(self.emails_file, 'w') as f:
                    json.dump(data, f, indent=2)
                
                self._invalidate_cache(f"emails_{user_id}")
                return True
            except:
                return False
    
    @cached(ttl_seconds=30)
    def get_emails(self, user_id: int) -> List[EmailAccount]:
        try:
            if not os.path.exists(self.emails_file):
                return []
            
            with open(self.emails_file, 'r') as f:
                data = json.load(f)
            
            user_str = str(user_id)
            if user_str not in data:
                return []
            
            accounts = []
            for acc_data in data[user_str]:
                acc_dict = {
                    'email': acc_data['email'],
                    'password': acc_data['password'],
                    'name': acc_data.get('name', ''),
                    'is_default': acc_data.get('is_default', False),
                    'last_auth_failure': acc_data.get('last_auth_failure', ''),
                    'auth_failure_count': acc_data.get('auth_failure_count', 0),
                    'connection_failure_count': acc_data.get('connection_failure_count', 0),
                    'last_connection_failure': acc_data.get('last_connection_failure', '')
                }
                accounts.append(EmailAccount(**acc_dict))
            return accounts
        except Exception as e:
            logging.error(f"❌ Failed to get emails for user {user_id}: {e}")
            return []
    
    def update_error_count(self, user_id: int, email: str, error_type: str, error_message: str):
        with self._lock:
            try:
                accounts = self.get_emails(user_id)
                updated = False
                
                for acc in accounts:
                    if acc.email == email:
                        if error_type == "authentication":
                            acc.last_auth_failure = datetime.now().isoformat()
                            acc.auth_failure_count += 1
                        elif error_type == "connection":
                            acc.last_connection_failure = datetime.now().isoformat()
                            acc.connection_failure_count += 1
                        updated = True
                        break
                
                if updated:
                    data = {}
                    if os.path.exists(self.emails_file):
                        with open(self.emails_file, 'r') as f:
                            data = json.load(f)
                    
                    user_str = str(user_id)
                    if user_str in data:
                        for idx, acc_data in enumerate(data[user_str]):
                            if acc_data['email'] == email:
                                if error_type == "authentication":
                                    data[user_str][idx]['last_auth_failure'] = datetime.now().isoformat()
                                    data[user_str][idx]['auth_failure_count'] = data[user_str][idx].get('auth_failure_count', 0) + 1
                                elif error_type == "connection":
                                    data[user_str][idx]['last_connection_failure'] = datetime.now().isoformat()
                                    data[user_str][idx]['connection_failure_count'] = data[user_str][idx].get('connection_failure_count', 0) + 1
                                break
                    
                    with open(self.emails_file, 'w') as f:
                        json.dump(data, f, indent=2)
                    
                    self._invalidate_cache(f"emails_{user_id}")
                return True
            except:
                return False
    
    def set_default(self, user_id: int, email: str) -> bool:
        with self._lock:
            accounts = self.get_emails(user_id)
            for acc in accounts:
                acc.is_default = (acc.email == email)
            
            try:
                data = {}
                if os.path.exists(self.emails_file):
                    with open(self.emails_file, 'r') as f:
                        data = json.load(f)
                
                user_str = str(user_id)
                data[user_str] = []
                for acc in accounts:
                    data[user_str].append({
                        'email': acc.email,
                        'password': acc.password,
                        'name': acc.name,
                        'is_default': acc.is_default,
                        'last_auth_failure': acc.last_auth_failure,
                        'auth_failure_count': acc.auth_failure_count,
                        'connection_failure_count': acc.connection_failure_count,
                        'last_connection_failure': acc.last_connection_failure
                    })
                
                with open(self.emails_file, 'w') as f:
                    json.dump(data, f, indent=2)
                
                self._invalidate_cache(f"emails_{user_id}")
                return True
            except:
                return False

# ===================== HTML VALIDATOR (OPTIMIZED) =====================

class HTMLValidator:
    """Enhanced HTML validator and cleaner with regex optimization"""
    
    _SCRIPT_TAG_PATTERN = re.compile(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', re.DOTALL | re.IGNORECASE)
    _EVENT_HANDLER_PATTERN = re.compile(r'\son\w+\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)
    _WHITESPACE_PATTERN = re.compile(r'>\s+<')
    
    @staticmethod
    def validate_html(html_content: str) -> tuple:
        if not html_content or len(html_content.strip()) == 0:
            return False, "❌ HTML content is empty", html_content
        
        has_any_tags = bool(re.search(r'<[^>]+>', html_content))
        opening_tags = len(re.findall(r'<[^/][^>]*>', html_content))
        closing_tags = len(re.findall(r'</[^>]+>', html_content))
        
        issues = []
        if opening_tags > 0 and closing_tags == 0:
            issues.append("ℹ️ No closing tags detected - sending as HTML fragment")
        
        cleaned_html = HTMLValidator.clean_html(html_content)
        
        if has_any_tags:
            message = "✅ HTML content detected"
            if opening_tags > 0 or closing_tags > 0:
                message += f" ({opening_tags} opening, {closing_tags} closing tags)"
        else:
            return True, "ℹ️ No HTML tags detected - sending as plain text", html_content
        
        if issues:
            message += "\n\n" + "\n".join(issues)
        
        return True, message, cleaned_html
    
    @staticmethod
    def clean_html(html_content: str) -> str:
        html_content = HTMLValidator._WHITESPACE_PATTERN.sub('>\n<', html_content)
        html_content = HTMLValidator._SCRIPT_TAG_PATTERN.sub('', html_content)
        html_content = HTMLValidator._EVENT_HANDLER_PATTERN.sub(' ', html_content)
        return html_content
    
    @staticmethod
    def extract_from_file(file_content: bytes) -> tuple:
        encodings = ['utf-8', 'windows-1252', 'latin-1']
        
        for encoding in encodings:
            try:
                html_content = file_content.decode(encoding)
                return True, "✅ File read successfully", html_content
            except UnicodeDecodeError:
                continue
        
        return False, "❌ Could not decode file. Please ensure it's a text file with valid encoding.", None

# ===================== ANTI-SPAM SERVICE (OPTIMIZED) =====================

class AntiSpamService:
    def __init__(self, telegram_app):
        self.app = telegram_app
        self.html_validator = HTMLValidator()
        self._connection_pool = {}
        self._pool_lock = threading.RLock()
    
    def _get_connection(self, sender: EmailAccount):
        """Get or create a cached SMTP connection"""
        key = f"{sender.email}:{SMTP_SERVER}:{SMTP_PORT}"
        
        with self._pool_lock:
            if key in self._connection_pool:
                conn, timestamp = self._connection_pool[key]
                if time.time() - timestamp < 60:  # Reuse connections for 60 seconds
                    try:
                        # Test connection
                        conn.noop()
                        return conn
                    except:
                        del self._connection_pool[key]
            
            # Create new connection
            if SMTP_PORT == 465:
                conn = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30)
            else:
                conn = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
                if SMTP_PORT == 587:
                    conn.starttls()
            
            conn.login(sender.email, sender.password)
            self._connection_pool[key] = (conn, time.time())
            return conn
    
    def _close_connection(self, conn):
        """Close SMTP connection"""
        try:
            conn.quit()
        except:
            pass
    
    def send_email(self, sender: EmailAccount, recipient_email: str, subject: str, body: str, is_html: bool = False, user_id: int = None) -> tuple:
        """Send email with anti-spam measures and connection reuse"""
        try:
            if is_html:
                is_valid, message, cleaned_html = self.html_validator.validate_html(body)
                if not is_valid:
                    return False, f"❌ HTML validation failed: {message}"
                body = cleaned_html
                msg = MIMEText(body, 'html', 'utf-8')
            else:
                msg = MIMEText(body, 'plain', 'utf-8')
            
            sender_display = f"{sender.name} <{sender.email}>" if sender.name else sender.email
            msg['From'] = sender_display
            msg['To'] = recipient_email
            msg['Subject'] = subject
            msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z')
            
            # Anti-spam headers
            msg['X-Priority'] = '3'
            msg['X-Mailer'] = 'Custom Mailer'
            msg['X-MSMail-Priority'] = 'Normal'
            msg['Importance'] = 'Normal'
            msg['List-Unsubscribe'] = f'<mailto:{sender.email}?subject=UNSUBSCRIBE>'
            msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
            
            domain = sender.email.split('@')[1] if '@' in sender.email else 'example.com'
            msg['Message-ID'] = f"<{time.time()}.{random.random()}@{domain}>"
            
            if is_html:
                msg.add_header('Content-Type', 'text/html; charset=utf-8')
            
            conn = self._get_connection(sender)
            conn.send_message(msg)
            
            time.sleep(random.uniform(0.5, 2.0))
            
            logging.info(f"✅ Email sent to {recipient_email}")
            return True, "Success"
                
        except (socket.timeout, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError) as e:
            error_msg = str(e)
            logging.error(f"🔌 Connection failed for {sender.email}: {error_msg}")
            with self._pool_lock:
                # Clear connection pool on error
                self._connection_pool.clear()
            return False, f"🔌 Connection error: {error_msg}"
                
        except smtplib.SMTPAuthenticationError as e:
            error_msg = str(e)
            logging.error(f"🔐 Authentication failed for {sender.email}: {error_msg}")
            
            if "535" in error_msg or "5.7.8" in error_msg:
                user_error_msg = "🔐 Password incorrect or not accepted by email provider"
            else:
                user_error_msg = f"🔐 Authentication failed: {error_msg}"
            
            return False, user_error_msg
            
        except smtplib.SMTPException as e:
            error_msg = str(e)
            logging.error(f"📧 SMTP error for {sender.email}: {error_msg}")
            
            if any(keyword in str(e).lower() for keyword in ['password', 'authentication', 'auth', 'login', '535', '5.7.8']):
                user_error_msg = "🔐 Password incorrect or authentication failed"
            else:
                user_error_msg = f"📧 SMTP error: {error_msg}"
            
            return False, user_error_msg
            
        except Exception as e:
            error_msg = str(e)
            logging.error(f"❌ Failed to send to {recipient_email}: {error_msg}")
            return False, f"❌ Error: {error_msg}"

# ===================== BULK EMAIL MANAGER (OPTIMIZED) =====================

class BulkEmailManager:
    def __init__(self, telegram_app, admin_id: int):
        self.storage = StorageService()
        self.email_service = AntiSpamService(telegram_app)
        self.admin_id = admin_id
        self.active_jobs = {}
        self.user_states = {}
        self.user_data = {}
        self._job_lock = threading.RLock()
    
    def start_step_process(self, user_id: int):
        self.user_states[user_id] = "waiting_emails"
        self.user_data[user_id] = {
            'recipients': [],
            'subject': "",
            'body': "",
            'is_html': False
        }
    
    def add_recipients(self, user_id: int, text: str) -> bool:
        if user_id not in self.user_data:
            return False
        
        emails = []
        lines = text.strip().split('\n')
        for line in lines:
            line = line.strip()
            if line and '@' in line and '.' in line.split('@')[-1]:
                emails.append(line)
        
        if not emails:
            return False
        
        self.user_data[user_id]['recipients'].extend(emails)
        return True
    
    def set_subject(self, user_id: int, subject: str) -> bool:
        if user_id in self.user_data:
            self.user_data[user_id]['subject'] = subject
            return True
        return False
    
    def set_body(self, user_id: int, body: str) -> bool:
        if user_id in self.user_data:
            self.user_data[user_id]['body'] = body
            return True
        return False
    
    def set_format(self, user_id: int, is_html: bool) -> bool:
        if user_id in self.user_data:
            self.user_data[user_id]['is_html'] = is_html
            return True
        return False
    
    def get_current_step(self, user_id: int) -> str:
        return self.user_states.get(user_id, "idle")
    
    def set_next_step(self, user_id: int, step: str):
        self.user_states[user_id] = step
    
    def clear_user_data(self, user_id: int):
        if user_id in self.user_states:
            del self.user_states[user_id]
        if user_id in self.user_data:
            del self.user_data[user_id]
    
    def start_bulk_send_from_steps(self, user_id: int, sender: EmailAccount) -> str:
        if user_id not in self.user_data:
            return "❌ No email data found. Start with /send"
        
        data = self.user_data[user_id]
        recipients = data['recipients']
        subject = data['subject']
        body = data['body']
        is_html = data['is_html']
        
        if not recipients:
            return "❌ No recipients provided"
        if not subject:
            return "❌ Subject is empty"
        if not body:
            return "❌ Body is empty"
        
        if is_html:
            is_valid, message, cleaned_body = self.email_service.html_validator.validate_html(body)
            if not is_valid:
                return f"❌ HTML validation failed:\n{message}"
            body = cleaned_body
        else:
            if len(body) < 50:
                return "❌ Body too short (min 50 chars for inbox)"
        
        if len(subject) < 5:
            return "❌ Subject too short"
        
        job = BulkEmailJob(
            user_id=user_id,
            sender=sender,
            recipient_list=recipients,
            subject=subject,
            body=body,
            is_html=is_html
        )
        
        with self._job_lock:
            self.active_jobs[user_id] = job
        
        job.thread = threading.Thread(target=self._send_emails, args=(user_id, job))
        job.thread.daemon = True
        job.thread.start()
        
        self.clear_user_data(user_id)
        
        format_status = "PURE HTML format" if is_html else "Plain text format"
        recipients_preview = "\n".join(recipients[:5])
        if len(recipients) > 5:
            recipients_preview += f"\n... and {len(recipients) - 5} more"
        
        return f"""✅ Starting bulk send!
📧 To: {len(recipients)} recipients
📝 Format: {format_status}
⏱️ Delay: {EMAIL_DELAY}s between emails
🛡️ Anti-spam: Enabled

📊 First 5 recipients:
{recipients_preview}"""
    
    def _send_emails(self, user_id: int, job: BulkEmailJob):
        job.is_running = True
        
        for i, recipient in enumerate(job.recipient_list):
            while job.is_paused and not job.should_stop:
                time.sleep(0.5)
            
            if job.should_stop:
                break
            
            success, error_msg = self.email_service.send_email(
                job.sender, recipient, job.subject, job.body, 
                is_html=job.is_html,
                user_id=user_id
            )
            
            if success:
                job.sent += 1
            else:
                job.failed += 1
                
                error_type = "general"
                if any(keyword in error_msg.lower() for keyword in ['password', 'authentication', 'incorrect', '535', '5.7.8', '🔐']):
                    error_type = "authentication"
                elif any(keyword in error_msg.lower() for keyword in ['connection', 'timeout', 'socket', 'connect', '🔌']):
                    error_type = "connection"
                elif any(keyword in error_msg.lower() for keyword in ['smtp', '📧']):
                    error_type = "smtp"
                
                job.error_summary[error_type] += 1
                
                email_error = EmailError(
                    recipient=recipient,
                    error_type=error_type,
                    error_message=error_msg,
                    timestamp=datetime.now().isoformat()
                )
                job.errors.append(email_error)
                
                if error_type in ["authentication", "connection"]:
                    self.storage.update_error_count(user_id, job.sender.email, error_type, error_msg)
            
            job.current_index = i + 1
            
            if i < len(job.recipient_list) - 1 and not job.should_stop:
                delay = EMAIL_DELAY + random.uniform(-2, 2)
                time.sleep(max(5, delay))
        
        job.is_running = False
        
        if job.errors:
            self._send_error_report_to_user(job)
        
        if job.failed >= 5 or any(count >= 3 for count in job.error_summary.values()):
            self._send_admin_alert(job)
        
        with self._job_lock:
            if user_id in self.active_jobs:
                del self.active_jobs[user_id]
    
    def _send_error_report_to_user(self, job: BulkEmailJob):
        try:
            report = f"""📊 EMAIL SENDING REPORT - COMPLETE

📧 Your Email Account: {job.sender.email}
👤 Account Name: {job.sender.name}
📅 Job Started: {datetime.fromtimestamp(job.start_time).strftime('%Y-%m-%d %H:%M:%S')}
🕒 Job Duration: {time.time() - job.start_time:.1f} seconds
📝 Format: {'PURE HTML' if job.is_html else 'Plain Text'}

📈 JOB STATISTICS:
• Total emails attempted: {len(job.recipient_list)}
• ✅ Successfully sent: {job.sent}
• ❌ Failed to send: {job.failed}
• 📊 Success Rate: {(job.sent/len(job.recipient_list)*100):.1f}%

🔴 ERROR SUMMARY:
"""
            
            if job.error_summary["authentication"] > 0:
                report += f"• 🔐 Authentication Errors: {job.error_summary['authentication']}\n"
            if job.error_summary["connection"] > 0:
                report += f"• 🔌 Connection Errors: {job.error_summary['connection']}\n"
            if job.error_summary["smtp"] > 0:
                report += f"• 📧 SMTP Errors: {job.error_summary['smtp']}\n"
            if job.error_summary["general"] > 0:
                report += f"• ❌ General Errors: {job.error_summary['general']}\n"
            
            report += "\n🔧 TROUBLESHOOTING GUIDE:\n"
            
            if job.error_summary["authentication"] > 0:
                report += """
🔐 AUTHENTICATION ERRORS:
1. Check if your password is correct
2. For Gmail users: Use App Password (not regular password)
   🔗 https://myaccount.google.com/apppasswords
3. Make sure "Less secure app access" is enabled
4. Check if your account is locked or requires verification
"""
            
            if job.error_summary["connection"] > 0:
                report += f"""
🔌 CONNECTION ERRORS:
1. Check your internet connection
2. SMTP server might be blocking your IP
3. Try using a VPN
4. Check if SMTP port {SMTP_PORT} is blocked
5. Server might be temporarily down
"""
            
            if job.error_summary["smtp"] > 0:
                report += """
📧 SMTP ERRORS:
1. Check recipient email addresses
2. Your account might have sending limits
3. Check for spam filter issues
4. Verify SMTP server settings
"""
            
            report += "\n📝 SAMPLE ERRORS (first 5):\n"
            for i, error in enumerate(job.errors[:5]):
                report += f"{i+1}. To: {error.recipient}\n"
                report += f"   Error: {error.error_message[:100]}...\n"
            
            if len(job.errors) > 5:
                report += f"\n... and {len(job.errors) - 5} more errors\n"
            
            report += "\n💡 RECOMMENDATIONS:\n"
            if job.error_summary["authentication"] > 0:
                report += "• Remove and re-add your email account with correct password\n"
            if job.error_summary["connection"] > 0:
                report += "• Try again later or use different network\n"
            report += "• Test with a single email first using the same settings\n"
            report += "• Check /accounts for account status\n"
            
            self._send_telegram_message(job.user_id, report)
            
            logging.info(f"📢 Sent comprehensive error report to user {job.user_id}")
            
        except Exception as e:
            logging.error(f"❌ Failed to send error report to user: {e}")
    
    def _send_admin_alert(self, job: BulkEmailJob):
        try:
            alert = f"""⚠️ BULK EMAIL JOB - MULTIPLE ERRORS DETECTED

👤 User ID: {job.user_id}
📧 Email: {job.sender.email}
👤 Name: {job.sender.name}
📅 Job Time: {datetime.fromtimestamp(job.start_time).strftime('%Y-%m-%d %H:%M:%S')}
📊 Results: {job.sent} sent, {job.failed} failed
📝 Format: {'PURE HTML' if job.is_html else 'Plain Text'}

🔴 ERROR BREAKDOWN:"""
            
            if job.error_summary["authentication"] > 0:
                alert += f"\n• 🔐 Authentication: {job.error_summary['authentication']}"
            if job.error_summary["connection"] > 0:
                alert += f"\n• 🔌 Connection: {job.error_summary['connection']}"
            if job.error_summary["smtp"] > 0:
                alert += f"\n• 📧 SMTP: {job.error_summary['smtp']}"
            if job.error_summary["general"] > 0:
                alert += f"\n• ❌ General: {job.error_summary['general']}"
            
            alert += f"\n\n✅ Action Taken:"
            alert += f"\n• User has been notified with detailed error report"
            alert += f"\n• User advised on troubleshooting steps"
            
            if job.error_summary["authentication"] > 0:
                alert += f"\n• Account marked with authentication warning"
            if job.error_summary["connection"] > 0:
                alert += f"\n• Account marked with connection warning"
            
            self._send_telegram_message(self.admin_id, alert)
            
        except Exception as e:
            logging.error(f"❌ Failed to send admin alert: {e}")
    
    def _send_telegram_message(self, chat_id: int, message: str):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            async def send_async():
                try:
                    await self.email_service.app.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.error(f"❌ Failed to send Telegram message to {chat_id}: {e}")
            
            loop.run_until_complete(send_async())
            loop.close()
            
        except Exception as e:
            logging.error(f"❌ Failed to create Telegram message task: {e}")
    
    def pause_job(self, user_id: int) -> str:
        with self._job_lock:
            if user_id in self.active_jobs and self.active_jobs[user_id].is_running:
                self.active_jobs[user_id].is_paused = True
                return "⏸️ Email sending paused"
        return "❌ No active job to pause"
    
    def resume_job(self, user_id: int) -> str:
        with self._job_lock:
            if user_id in self.active_jobs and self.active_jobs[user_id].is_running:
                self.active_jobs[user_id].is_paused = False
                return "▶️ Email sending resumed"
        return "❌ No paused job to resume"
    
    def stop_job(self, user_id: int) -> str:
        with self._job_lock:
            if user_id in self.active_jobs:
                self.active_jobs[user_id].should_stop = True
                return "🛑 Stopping email send..."
        return "❌ No active job to stop"
    
    def get_status(self, user_id: int) -> str:
        if user_id not in self.active_jobs:
            step = self.get_current_step(user_id)
            if step != "idle":
                data = self.user_data.get(user_id, {})
                if step == "waiting_emails":
                    return f"📝 Waiting for recipient emails...\n📧 Current count: {len(data.get('recipients', []))} emails"
                elif step == "waiting_subject":
                    return f"📝 Waiting for subject...\n📧 Recipients: {len(data.get('recipients', []))} emails"
                elif step == "waiting_format":
                    return "📝 Please choose email format (HTML or Plain Text) using the buttons above"
                elif step == "waiting_body":
                    return f"📝 Waiting for {'HTML content' if data.get('is_html') else 'body'}...\n📧 Recipients: {len(data.get('recipients', []))} emails\n📝 Subject: {data.get('subject', '')[:50]}..."
                elif step == "waiting_html_file":
                    return f"📝 Waiting for HTML file upload...\n📧 Recipients: {len(data.get('recipients', []))} emails\n📝 Subject: {data.get('subject', '')[:50]}..."
            return "📭 No active email job"
        
        job = self.active_jobs[user_id]
        total = len(job.recipient_list)
        progress = f"{job.current_index}/{total}"
        percent = (job.current_index / total * 100) if total > 0 else 0
        
        status = f"👤 User: {job.user_id}\n"
        status += f"📊 Status: {'Paused' if job.is_paused else 'Running' if job.is_running else 'Completed'}\n"
        status += f"📝 Format: {'PURE HTML' if job.is_html else 'Plain Text'}\n"
        status += f"📨 Progress: {progress} ({percent:.1f}%)\n"
        status += f"✅ Sent: {job.sent}\n"
        status += f"❌ Failed: {job.failed}\n"
        
        if job.failed > 0:
            status += f"🔴 Errors: {job.failed} (you'll receive Telegram report)\n"
            if job.error_summary["authentication"] > 0:
                status += f"   🔐 Auth: {job.error_summary['authentication']}\n"
            if job.error_summary["connection"] > 0:
                status += f"   🔌 Connection: {job.error_summary['connection']}\n"
        
        status += f"⏱️ Delay: {EMAIL_DELAY}s between emails\n"
        status += f"🛡️ Anti-spam: Enabled\n"
        
        if job.current_index < total:
            remaining = total - job.current_index
            eta_seconds = remaining * EMAIL_DELAY
            if eta_seconds > 3600:
                status += f"⏳ ETA: {eta_seconds/3600:.1f} hours\n"
            elif eta_seconds > 60:
                status += f"⏳ ETA: {eta_seconds/60:.1f} minutes\n"
            else:
                status += f"⏳ ETA: {eta_seconds} seconds\n"
        
        return status

# ===================== TELEGRAM BOT (OPTIMIZED) =====================

class EmailBot:
    def __init__(self, token: str, admin_id: int):
        self.token = token
        self.admin_id = admin_id
        self.app = Application.builder().token(token).build()
        self.manager = BulkEmailManager(self.app, admin_id)
        self.setup_handlers()
    
    def setup_handlers(self):
        handlers = [
            CommandHandler("start", self.start_cmd),
            CommandHandler("help", self.help_cmd),
            CommandHandler("auth", self.auth_cmd),
            CommandHandler("mycode", self.mycode_cmd),
            CommandHandler("add", self.add_email_cmd),
            CommandHandler("accounts", self.list_accounts_cmd),
            CommandHandler("default", self.set_default_cmd),
            CommandHandler("send", self.send_cmd),
            CommandHandler("status", self.status_cmd),
            CommandHandler("pause", self.pause_cmd),
            CommandHandler("resume", self.resume_cmd),
            CommandHandler("stop", self.stop_cmd),
            CommandHandler("clear", self.clear_cmd),
            CommandHandler("cancel", self.cancel_cmd),
            CommandHandler("generate", self.generate_code_cmd),
            CommandHandler("codes", self.list_codes_cmd),
            CommandHandler("activecodes", self.active_codes_cmd),
            CommandHandler("inactivecodes", self.inactive_codes_cmd),
            CommandHandler("activeusers", self.active_users_cmd),
            CommandHandler("checkcode", self.check_code_cmd),
            CommandHandler("togglecode", self.toggle_code_cmd),
            CommandHandler("activatecode", self.activate_with_users_cmd),
            CommandHandler("codenote", self.code_note_cmd),
            CommandHandler("users", self.list_users_cmd),
            CommandHandler("check_auth", self.check_auth_cmd),
            CommandHandler("adminhelp", self.admin_help_cmd),
            CommandHandler("admincommands", self.admin_help_cmd),
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text),
            MessageHandler(filters.Document.ALL, self.handle_document),
            CallbackQueryHandler(self.handle_callback),
        ]
        
        for handler in handlers:
            self.app.add_handler(handler)
    
    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name or "User"
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text(
                f"👋 Welcome {username}!\n\n"
                "🔒 ACCESS REQUIRED\n\n"
                "This bot requires an access code to use.\n\n"
                "To get access:\n"
                "1. Contact the admin for an access code\n"
                "2. Use /auth YOUR_CODE to activate\n\n"
                "Example:\n"
                "/auth ABC123"
            )
            return
        
        if self.manager.storage.is_admin(user_id):
            greeting = f"👑 Welcome back, Admin {username}!\n\n"
        else:
            user_data = self.manager.storage.authorized_users.get(str(user_id), {})
            code_used = user_data.get('authorized_by', 'Unknown')
            code_info = self.manager.storage.access_codes.get(code_used, {})
            users_used = len(code_info.get('users_used', []))
            max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
            remaining = max_users - users_used
            
            greeting = f"👋 Welcome back, {username}!\n"
            greeting += f"🔑 Your code: {code_used}\n"
            greeting += f"📊 Remaining uses: {remaining}\n\n"
        
        help_text = greeting + """
📧 ADVANCED BULK EMAIL BOT 📧

⚡ QUICK START:
1. /add - Add your email account
2. /send - Start step-by-step email creation

🎯 STEP-BY-STEP PROCESS:
When you use /send:
1. Send recipient emails
2. Send email subject
3. Choose format (HTML or Plain Text)
4. Send email body (or upload .txt file for HTML)

🛡️ ANTI-SPAM FEATURES:
• Proper email headers
• PURE HTML support
• Unsubscribe links
• Gradual sending (10s delay)
• Random timing variations

📧 PURE HTML SUPPORT:
• Choose HTML format after subject
• Upload .txt file containing HTML code
• Sent as pure HTML (no plain text fallback)

⚠️ COMPREHENSIVE ERROR REPORTING:
• 🔐 Authentication errors (password issues)
• 🔌 Connection errors (network/SMTP timeouts)
• 📧 SMTP errors (server issues)
• ❌ General errors
• All errors reported to Telegram with detailed troubleshooting

🎯 COMMANDS:
/auth CODE - Activate with access code
/mycode - Check your code status
/add - Add email account
/accounts - List your accounts
/default - Set default account
/send - Start step-by-step email creation
/status - Check progress
/pause - Pause sending
/resume - Resume sending
/stop - Stop completely
/cancel - Cancel current operation
/clear - Clear your data

⏱️ 10-second delay between each email
👥 Multi-user support
"""
        
        if self.manager.storage.is_admin(user_id):
            help_text += f"""

🔧 ADMIN COMMANDS:
/generate - Generate new access code
/codes - List all access codes
/activecodes - List only active codes
/inactivecodes - List only inactive codes
/activeusers - List all active users
/checkcode CODE - Check specific code details
/togglecode CODE - Toggle code active/inactive
/activatecode CODE USERS - Activate code with existing users
/codenote CODE NOTE - Add note to a code
/users - List all authorized users
/check_auth - Check email authentication & connection status
/adminhelp or /admincommands - Show all admin commands
"""
        
        await update.message.reply_text(help_text)
    
    async def auth_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.manager.storage.is_authorized(user_id):
            user_data = self.manager.storage.authorized_users.get(str(user_id), {})
            code_used = user_data.get('authorized_by', 'Unknown')
            authorized_at = user_data.get('authorized_at', 'Unknown')
            
            if self.manager.storage.is_admin(user_id):
                await update.message.reply_text("👑 You are already admin!")
            else:
                code_info = self.manager.storage.access_codes.get(code_used, {})
                users_used = len(code_info.get('users_used', []))
                max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
                remaining = max_users - users_used
                
                response = f"✅ You are already authorized!\n\n"
                response += f"🔑 Code used: {code_used}\n"
                response += f"📅 Authorized: {authorized_at[:10] if len(authorized_at) > 10 else authorized_at}\n"
                response += f"👥 Code usage: {users_used}/{max_users} users\n"
                response += f"📊 Remaining uses: {remaining}\n\n"
                response += f"Type /start to see available commands"
                
                await update.message.reply_text(response)
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ Usage: /auth ACCESS_CODE\n\n"
                "Example: /auth ABC123\n\n"
                "Contact admin for an access code."
            )
            return
        
        code = context.args[0]
        success, message = self.manager.storage.verify_access_code(code, user_id)
        
        if success:
            code_info = self.manager.storage.access_codes.get(code.upper(), {})
            users_used = len(code_info.get('users_used', []))
            max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
            remaining = max_users - users_used
            
            message += f"\n\n📊 CODE STATUS:\n"
            message += f"• Code: {code.upper()}\n"
            message += f"• Users used: {users_used}/{max_users}\n"
            message += f"• Remaining uses: {remaining}\n\n"
            message += f"💡 You are user #{users_used} for this code"
        
        await update.message.reply_text(message)
    
    async def mycode_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        if self.manager.storage.is_admin(user_id):
            await update.message.reply_text("👑 You are admin - no access code needed!")
            return
        
        user_data = self.manager.storage.authorized_users.get(str(user_id), {})
        code_used = user_data.get('authorized_by', 'Unknown')
        authorized_at = user_data.get('authorized_at', 'Unknown')
        
        code_info = self.manager.storage.access_codes.get(code_used, {})
        users_used = len(code_info.get('users_used', []))
        max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
        remaining = max_users - users_used
        is_active = code_info.get('is_active', True)
        status = "🟢 ACTIVE" if is_active else "🔴 INACTIVE"
        
        response = f"📊 YOUR ACCESS CODE STATUS\n\n"
        response += f"🔑 Code: {code_used}\n"
        response += f"📊 Status: {status}\n"
        response += f"📅 Authorized: {authorized_at[:10] if len(authorized_at) > 10 else authorized_at}\n"
        response += f"👥 Total users for this code: {users_used}/{max_users}\n"
        response += f"📈 Remaining uses: {remaining}\n"
        
        if code_used != 'Unknown':
            try:
                users_list = code_info.get('users_used', [])
                if user_id in users_list:
                    position = users_list.index(user_id) + 1
                    response += f"👤 Your position: #{position}\n"
            except:
                pass
        
        await update.message.reply_text(response)
    
    async def generate_code_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        code = self.manager.storage.generate_access_code(user_id)
        
        if code:
            await update.message.reply_text(
                f"✅ NEW ACCESS CODE GENERATED!\n\n"
                f"🔑 Code: {code}\n"
                f"⏰ Created: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"👥 Max users: {MAX_USERS_PER_CODE}\n\n"
                f"Share this code with users:\n"
                f"`/auth {code}`"
            )
        else:
            await update.message.reply_text("❌ Failed to generate code")
    
    async def list_codes_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        codes_list = self.manager.storage.list_codes(user_id)
        await update.message.reply_text(codes_list)
    
    async def active_codes_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        active_codes = self.manager.storage.get_active_codes()
        
        if not active_codes:
            await update.message.reply_text("🔴 No active codes found.")
            return
        
        result = "🟢 ACTIVE ACCESS CODES:\n\n"
        for code in active_codes[:20]:
            code_info = self.manager.storage.get_code_info(code)
            if code_info:
                users_used = len(code_info.get('users_used', []))
                max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
                created_at = code_info.get('created_at', 'Unknown')
                notes = code_info.get('notes', '')
                
                result += f"🔑 Code: {code}\n"
                result += f"   👥 Usage: {users_used}/{max_users} users\n"
                result += f"   📊 Remaining: {max_users - users_used} users\n"
                result += f"   📅 Created: {created_at[:10] if len(created_at) > 10 else created_at}\n"
                if notes:
                    result += f"   📝 Notes: {notes[:50]}...\n" if len(notes) > 50 else f"   📝 Notes: {notes}\n"
                result += "─" * 40 + "\n"
        
        if len(active_codes) > 20:
            result += f"\n... and {len(active_codes) - 20} more active codes"
        
        await update.message.reply_text(result)
    
    async def inactive_codes_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        inactive_codes = self.manager.storage.get_inactive_codes()
        
        if not inactive_codes:
            await update.message.reply_text("🟢 No inactive codes found.")
            return
        
        result = "🔴 INACTIVE ACCESS CODES:\n\n"
        for code in inactive_codes[:20]:
            code_info = self.manager.storage.get_code_info(code)
            if code_info:
                users_used = len(code_info.get('users_used', []))
                max_users = code_info.get('max_users', MAX_USERS_PER_CODE)
                created_at = code_info.get('created_at', 'Unknown')
                notes = code_info.get('notes', '')
                
                result += f"🔑 Code: {code}\n"
                result += f"   👥 Usage: {users_used}/{max_users} users\n"
                result += f"   📊 Remaining: {max_users - users_used} users\n"
                result += f"   📅 Created: {created_at[:10] if len(created_at) > 10 else created_at}\n"
                if notes:
                    result += f"   📝 Notes: {notes[:50]}...\n" if len(notes) > 50 else f"   📝 Notes: {notes}\n"
                result += "─" * 40 + "\n"
        
        if len(inactive_codes) > 20:
            result += f"\n... and {len(inactive_codes) - 20} more inactive codes"
        
        await update.message.reply_text(result)
    
    async def active_users_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        active_users = self.manager.storage.get_active_users()
        
        if not active_users:
            await update.message.reply_text("👤 No active users found.")
            return
        
        result = "👤 ACTIVE USERS:\n\n"
        
        code_counts = {}
        for user_id_str, user_data in active_users.items():
            code_used = user_data.get('authorized_by', 'Unknown')
            code_counts[code_used] = code_counts.get(code_used, 0) + 1
        
        result += f"📊 Total active users: {len(active_users)}\n\n"
        result += "📋 USERS BY CODE:\n"
        for code, count in sorted(code_counts.items()):
            result += f"• {code}: {count} user(s)\n"
        
        result += "\n👥 USER DETAILS:\n"
        for i, (user_id_str, user_data) in enumerate(list(active_users.items())[:20], 1):
            code_used = user_data.get('authorized_by', 'Unknown')
            authorized_at = user_data.get('authorized_at', 'Unknown')
            username = user_data.get('username', 'Unknown')
            
            result += f"\n{i}. 👤 User ID: {user_id_str}\n"
            result += f"   🔑 Code used: {code_used}\n"
            result += f"   📅 Authorized: {authorized_at[:10] if len(authorized_at) > 10 else authorized_at}\n"
            if username and username != 'Unknown':
                result += f"   📝 Username: {username}\n"
        
        if len(active_users) > 20:
            result += f"\n... and {len(active_users) - 20} more users"
        
        await update.message.reply_text(result)
    
    async def check_code_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ Usage: /checkcode CODE\n\n"
                "Example: /checkcode OWK475\n\n"
                "This will show detailed information about the code including all users who used it."
            )
            return
        
        code = context.args[0]
        code_info = self.manager.storage.check_code_status(code)
        await update.message.reply_text(code_info)
    
    async def toggle_code_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text(
                "❌ Usage: /togglecode CODE\n\n"
                "Example: /togglecode OWK475\n\n"
                "This will activate/deactivate the code."
            )
            return
        
        code = context.args[0]
        result = self.manager.storage.toggle_code_status(code, user_id)
        await update.message.reply_text(result)
    
    async def activate_with_users_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Usage: /activatecode CODE USERS_COUNT\n\n"
                "Example: /activatecode OWK475 3\n\n"
                "This will:\n"
                "1. Activate the code\n"
                "2. Mark that 3 users have already used it\n"
                "3. Update the remaining uses accordingly\n\n"
                "Note: USERS_COUNT must be 0 or more"
            )
            return
        
        code = context.args[0]
        try:
            users_count = int(context.args[1])
            if users_count < 0:
                raise ValueError
        except:
            await update.message.reply_text("❌ Invalid users count. Please enter a number 0 or greater.")
            return
        
        result = self.manager.storage.activate_code_with_users(code, users_count, user_id)
        await update.message.reply_text(result)
    
    async def code_note_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Usage: /codenote CODE NOTE\n\n"
                "Example: /codenote OWK475 No longer working - use ABC123 instead\n\n"
                "This will add a note to the code for future reference."
            )
            return
        
        code = context.args[0]
        note = ' '.join(context.args[1:])
        result = self.manager.storage.add_code_note(code, note, user_id)
        await update.message.reply_text(result)
    
    async def list_users_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        users_list = self.manager.storage.list_users(user_id)
        await update.message.reply_text(users_list)
    
    async def check_auth_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        all_accounts = []
        auth_issues = []
        connection_issues = []
        
        try:
            if os.path.exists(self.manager.storage.emails_file):
                with open(self.manager.storage.emails_file, 'r') as f:
                    data = json.load(f)
                
                for user_str, accounts in data.items():
                    user_id_int = int(user_str)
                    for acc in accounts:
                        acc_obj = EmailAccount(**acc)
                        all_accounts.append({
                            'user_id': user_id_int,
                            'account': acc_obj
                        })
                        
                        if acc_obj.auth_failure_count > 0:
                            auth_issues.append({
                                'user_id': user_id_int,
                                'email': acc_obj.email,
                                'name': acc_obj.name,
                                'failure_count': acc_obj.auth_failure_count,
                                'last_failure': acc_obj.last_auth_failure
                            })
                        
                        if acc_obj.connection_failure_count > 0:
                            connection_issues.append({
                                'user_id': user_id_int,
                                'email': acc_obj.email,
                                'name': acc_obj.name,
                                'failure_count': acc_obj.connection_failure_count,
                                'last_failure': acc_obj.last_connection_failure
                            })
        except:
            pass
        
        if not auth_issues and not connection_issues:
            await update.message.reply_text("✅ No authentication or connection issues found.")
            return
        
        report = f"🔍 EMAIL ACCOUNT STATUS REPORT\n\n"
        
        if auth_issues:
            report += f"🔐 AUTHENTICATION ISSUES: {len(auth_issues)} accounts\n\n"
            for i, issue in enumerate(auth_issues[:10], 1):
                report += f"{i}. 📧 {issue['email']}\n"
                report += f"   👤 User ID: {issue['user_id']}\n"
                report += f"   📝 Name: {issue['name']}\n"
                report += f"   🔴 Failures: {issue['failure_count']}\n"
                report += f"   ⏰ Last failure: {issue['last_failure'][:19] if issue['last_failure'] else 'Never'}\n"
                report += "─" * 30 + "\n"
            
            if len(auth_issues) > 10:
                report += f"\n... and {len(auth_issues) - 10} more accounts with auth issues\n\n"
        
        if connection_issues:
            report += f"\n🔌 CONNECTION ISSUES: {len(connection_issues)} accounts\n\n"
            for i, issue in enumerate(connection_issues[:10], 1):
                report += f"{i}. 📧 {issue['email']}\n"
                report += f"   👤 User ID: {issue['user_id']}\n"
                report += f"   📝 Name: {issue['name']}\n"
                report += f"   🔴 Failures: {issue['failure_count']}\n"
                report += f"   ⏰ Last failure: {issue['last_failure'][:19] if issue['last_failure'] else 'Never'}\n"
                report += "─" * 30 + "\n"
            
            if len(connection_issues) > 10:
                report += f"\n... and {len(connection_issues) - 10} more accounts with connection issues"
        
        await update.message.reply_text(report)
    
    async def admin_help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_admin(user_id):
            await update.message.reply_text("❌ Admin only command!")
            return
        
        admin_help = f"""👑 ADMIN COMMAND REFERENCE 👑

📋 CODE MANAGEMENT:
/generate - Generate new access code
• Generates a new 6-character access code (ABC123 format)
• Max {MAX_USERS_PER_CODE} users per code
• Usage: /generate

/codes - List all access codes
• Shows all generated codes with status and usage
• Shows remaining uses for each code
• Usage: /codes

/activecodes - List only active codes
• Shows only active codes with usage statistics
• Usage: /activecodes

/inactivecodes - List only inactive codes
• Shows only inactive codes that can be reactivated
• Usage: /inactivecodes

/checkcode CODE - Check specific code details
• Shows detailed info about a specific code
• Lists all users who used the code
• Shows remaining uses and status
• Usage: /checkcode OWK475

/togglecode CODE - Toggle code active/inactive
• Activate or deactivate a code
• Usage: /togglecode OWK475

/activatecode CODE USERS - Activate code with existing users
• Activate a code and specify how many users already used it
• Usage: /activatecode OWK475 3

/codenote CODE NOTE - Add note to a code
• Add notes for code management
• Usage: /codenote OWK475 "No longer working"

📊 USER MANAGEMENT:
/users - List all authorized users
• Shows all users who have access
• Shows which code they used
• Usage: /users

/activeusers - List all active users
• Shows all current active users with details
• Shows user distribution by code
• Usage: /activeusers

🛠️ SYSTEM MANAGEMENT:
/check_auth - Check email authentication issues
• Shows email accounts with authentication problems
• Shows connection failure counts
• Usage: /check_auth

📁 DATA MANAGEMENT:
All data is automatically saved to:
• access_data.json - Access codes and user permissions
• emails.json - User email accounts and error history

⚙️ CONFIGURATION (in script):
• ADMIN_USER_ID = {ADMIN_USER_ID}
• MAX_USERS_PER_CODE = {MAX_USERS_PER_CODE}
• EMAIL_DELAY = {EMAIL_DELAY} seconds
• SMTP_SERVER = {SMTP_SERVER}:{SMTP_PORT}

💡 TIPS:
1. Always check /codes before giving out codes
2. Use /checkcode to monitor specific codes
3. Add notes to codes for future reference
4. Check /users to see who has access
5. Use /check_auth to troubleshoot email issues

🔐 SPECIFIC CODES TRACKED:
• OWK475 - Pre-added (inactive by default)
• All codes saved in access_data.json
"""
        
        await update.message.reply_text(admin_help)
    
    async def add_email_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        context.user_data['adding_email'] = True
        context.user_data['step'] = "name"
        await update.message.reply_text("What's your name? (Will appear as sender):")
    
    async def list_accounts_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        accounts = self.manager.storage.get_emails(user_id)
        
        if not accounts:
            await update.message.reply_text("❌ No email accounts. Use /add")
            return
        
        text = "📧 YOUR ACCOUNTS:\n\n"
        for i, acc in enumerate(accounts, 1):
            default = " ✅ DEFAULT" if acc.is_default else ""
            auth_warning = f" 🔐 {acc.auth_failure_count} auth failures" if acc.auth_failure_count > 0 else ""
            connection_warning = f" 🔌 {acc.connection_failure_count} connection failures" if acc.connection_failure_count > 0 else ""
            
            text += f"{i}. {acc.name} <{acc.email}>{default}{auth_warning}{connection_warning}\n"
        
        text += "\n🔍 LEGEND:\n"
        text += "🔐 = Authentication/password errors\n"
        text += "🔌 = Connection/timeout errors\n"
        text += "✅ = Default sending account\n"
        
        await update.message.reply_text(text)
    
    async def set_default_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        accounts = self.manager.storage.get_emails(user_id)
        
        if not accounts:
            await update.message.reply_text("❌ No accounts to choose")
            return
        
        buttons = []
        for acc in accounts:
            btn_text = f"{acc.email}"
            if acc.is_default:
                btn_text += " ✅"
            if acc.auth_failure_count > 0:
                btn_text += " 🔐"
            if acc.connection_failure_count > 0:
                btn_text += " 🔌"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"setdef_{acc.email}")])
        
        markup = InlineKeyboardMarkup(buttons)
        await update.message.reply_text("Select default account:", reply_markup=markup)
    
    async def send_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        accounts = self.manager.storage.get_emails(user_id)
        
        if not accounts:
            await update.message.reply_text("❌ No email accounts. Use /add first")
            return
        
        sender = None
        for acc in accounts:
            if acc.is_default:
                sender = acc
                break
        if not sender:
            sender = accounts[0]
        
        warning = ""
        if sender.auth_failure_count > 0:
            warning = f"\n⚠️ WARNING: This account has {sender.auth_failure_count} authentication failures!\n"
            warning += "You may need to update your password. Check /accounts for details.\n\n"
        
        if sender.connection_failure_count > 0:
            warning += f"\n⚠️ WARNING: This account has {sender.connection_failure_count} connection failures!\n"
            warning += "Network/SMTP connection issues detected. You may need to check your network.\n\n"
        
        self.manager.start_step_process(user_id)
        
        await update.message.reply_text(
            f"{warning}"
            f"📧 STEP 1: RECIPIENT EMAILS\n\n"
            f"Sending from: {sender.name} <{sender.email}>\n\n"
            f"🔔 IMPORTANT: You'll receive a comprehensive Telegram report after the job completes!\n\n"
            f"Send me the recipient email addresses:\n\n"
            f"📝 FORMAT:\n"
            f"• One email per line:\n"
            f"  email1@example.com\n"
            f"  email2@example.com\n\n"
            f"• Or comma separated:\n"
            f"  email1@example.com, email2@example.com\n\n"
            f"Type /cancel to stop this process"
        )
    
    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        status = self.manager.get_status(user_id)
        await update.message.reply_text(status)
    
    async def pause_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        result = self.manager.pause_job(user_id)
        await update.message.reply_text(result)
    
    async def resume_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        result = self.manager.resume_job(user_id)
        await update.message.reply_text(result)
    
    async def stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        result = self.manager.stop_job(user_id)
        await update.message.reply_text(result)
    
    async def cancel_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        self.manager.clear_user_data(user_id)
        if 'adding_email' in context.user_data:
            context.user_data.clear()
        
        await update.message.reply_text("✅ Operation cancelled.")
    
    async def clear_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            await update.message.reply_text("❌ Not authorized. Use /auth CODE first")
            return
        
        self.manager.clear_user_data(user_id)
        
        try:
            if os.path.exists("emails.json"):
                with open("emails.json", 'r') as f:
                    data = json.load(f)
                
                user_str = str(user_id)
                if user_str in data:
                    del data[user_str]
                
                with open("emails.json", 'w') as f:
                    json.dump(data, f, indent=2)
        except:
            pass
        
        await update.message.reply_text("✅ All your data cleared!")
    
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not self.manager.storage.is_authorized(user_id):
            return
        
        step = self.manager.get_current_step(user_id)
        if step != "waiting_html_file":
            await update.message.reply_text("❌ Not expecting a file. Please follow the steps or use /cancel")
            return
        
        document = update.message.document
        
        if not document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Please upload a .txt file containing your HTML code")
            return
        
        file = await document.get_file()
        file_content = await file.download_as_bytearray()
        
        success, message, html_content = HTMLValidator.extract_from_file(file_content)
        
        if not success:
            await update.message.reply_text(message)
            return
        
        is_valid, validation_message, cleaned_html = HTMLValidator.validate_html(html_content)
        
        if not is_valid:
            await update.message.reply_text(f"❌ HTML validation failed:\n{validation_message}")
            return
        
        if self.manager.set_format(user_id, True):
            if self.manager.set_body(user_id, cleaned_html):
                accounts = self.manager.storage.get_emails(user_id)
                sender = None
                for acc in accounts:
                    if acc.is_default:
                        sender = acc
                        break
                if not sender:
                    sender = accounts[0]
                
                result = self.manager.start_bulk_send_from_steps(user_id, sender)
                await update.message.reply_text(result)
            else:
                await update.message.reply_text("❌ Failed to save HTML content")
        else:
            await update.message.reply_text("❌ Failed to set HTML format")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        
        if not self.manager.storage.is_authorized(user_id):
            return
        
        if context.user_data.get('adding_email'):
            step = context.user_data.get('step')
            
            if step == "name":
                if len(text) < 2:
                    await update.message.reply_text("❌ Name too short. Enter your name:")
                    return
                
                context.user_data['step'] = "email"
                context.user_data['temp_name'] = text
                await update.message.reply_text(f"Name: {text}\n\nNow enter your email address:")
            
            elif step == "email":
                if '@' not in text or '.' not in text.split('@')[-1]:
                    await update.message.reply_text("❌ Invalid email. Try again:")
                    return
                
                context.user_data['step'] = "password"
                context.user_data['temp_email'] = text
                await update.message.reply_text(
                    f"Email: {text}\n\n"
                    f"Enter password (for Gmail use App Password):\n\n"
                    f"⚠️ IMPORTANT: All errors will be reported to Telegram!\n"
                    f"• 🔐 Password errors\n"
                    f"• 🔌 Connection errors\n"
                    f"• 📧 SMTP errors\n"
                    f"• ❌ All other errors\n\n"
                    f"🔗 Gmail App Password: https://myaccount.google.com/apppasswords"
                )
            
            elif step == "password":
                name = context.user_data.get('temp_name', '')
                email = context.user_data.get('temp_email', '')
                password = text
                
                accounts = self.manager.storage.get_emails(user_id)
                is_default = (len(accounts) == 0)
                
                account = EmailAccount(
                    email=email,
                    password=password,
                    name=name,
                    is_default=is_default
                )
                
                if self.manager.storage.save_email(user_id, account):
                    response = f"✅ Account added!\n👤 {name}\n📧 {email}"
                    if is_default:
                        response += "\n⭐ Set as default"
                    response += "\n\n🔔 You'll receive Telegram reports for all errors!"
                    await update.message.reply_text(response)
                else:
                    await update.message.reply_text("❌ Failed to save account")
                
                context.user_data.clear()
            
            return
        
        step = self.manager.get_current_step(user_id)
        
        if step == "waiting_emails":
            if self.manager.add_recipients(user_id, text):
                self.manager.set_next_step(user_id, "waiting_subject")
                
                data = self.manager.user_data[user_id]
                await update.message.reply_text(
                    f"✅ Received {len(data['recipients'])} email(s)\n\n"
                    f"📧 STEP 2: EMAIL SUBJECT\n\n"
                    f"Now send me the email subject:\n\n"
                    f"💡 Tips:\n"
                    f"• Keep it clear and concise\n"
                    f"• Avoid spammy words\n"
                    f"• 5-10 words recommended\n\n"
                    f"🔔 Remember: Complete error report will be sent to Telegram!"
                )
            else:
                await update.message.reply_text(
                    "❌ No valid emails found.\n\n"
                    "Please send valid email addresses:\n"
                    "• One per line\n"
                    "• Or comma separated\n\n"
                    "Example:\n"
                    "user1@example.com\n"
                    "user2@example.com"
                )
        
        elif step == "waiting_subject":
            if len(text) < 2:
                await update.message.reply_text(
                    "❌ Subject too short.\n"
                    "Please enter a proper subject (min 2 characters):"
                )
                return
            
            if self.manager.set_subject(user_id, text):
                self.manager.set_next_step(user_id, "waiting_format")
                
                keyboard = [
                    [InlineKeyboardButton("📝 Plain Text", callback_data=f"format_plain_{user_id}")],
                    [InlineKeyboardButton("🎨 HTML (Pure)", callback_data=f"format_html_{user_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"✅ Subject: {text}\n\n"
                    f"📧 STEP 3: CHOOSE FORMAT\n\n"
                    f"Please select the email format:",
                    reply_markup=reply_markup
                )
        
        elif step == "waiting_body":
            if len(text) < 50:
                await update.message.reply_text(
                    "❌ Body too short (minimum 50 characters).\n"
                    "Please provide a proper email body:"
                )
                return
            
            if self.manager.set_body(user_id, text):
                accounts = self.manager.storage.get_emails(user_id)
                sender = None
                for acc in accounts:
                    if acc.is_default:
                        sender = acc
                        break
                if not sender:
                    sender = accounts[0]
                
                result = self.manager.start_bulk_send_from_steps(user_id, sender)
                await update.message.reply_text(result)
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        data = query.data
        
        if data.startswith("setdef_"):
            if not self.manager.storage.is_authorized(user_id):
                await query.edit_message_text("❌ Not authorized. Use /auth CODE first")
                return
            
            email = data.replace("setdef_", "")
            if self.manager.storage.set_default(user_id, email):
                await query.edit_message_text(f"✅ Default set to: {email}")
            else:
                await query.edit_message_text("❌ Failed to set default")
        
        elif data.startswith("format_"):
            if not self.manager.storage.is_authorized(user_id):
                await query.edit_message_text("❌ Not authorized. Use /auth CODE first")
                return
            
            parts = data.split('_')
            if len(parts) == 3:
                format_type = parts[1]
                callback_user_id = int(parts[2])
                
                if callback_user_id != user_id:
                    await query.edit_message_text("❌ This selection is not for you")
                    return
                
                is_html = (format_type == "html")
                step = self.manager.get_current_step(user_id)
                
                if step != "waiting_format":
                    await query.edit_message_text("❌ Not expecting format selection. Please start over with /send")
                    return
                
                if self.manager.set_format(user_id, is_html):
                    self.manager.set_next_step(user_id, "waiting_body" if not is_html else "waiting_html_file")
                    
                    if is_html:
                        await query.edit_message_text(
                            f"✅ Format set to: PURE HTML\n\n"
                            f"📧 STEP 4: UPLOAD HTML FILE\n\n"
                            f"Please upload your HTML code as a .txt file:\n\n"
                            f"📝 INSTRUCTIONS:\n"
                            f"1. Save your HTML code in a text file with .txt extension\n"
                            f"2. Upload the file here\n\n"
                            f"✅ The bot will automatically:\n"
                            f"• Validate your HTML (warnings only)\n"
                            f"• Clean and optimize it\n"
                            f"• Send as PURE HTML (no text fallback)"
                        )
                    else:
                        await query.edit_message_text(
                            f"✅ Format set to: Plain Text\n\n"
                            f"📧 STEP 4: EMAIL BODY\n\n"
                            f"Now send me the email body/content:\n\n"
                            f"📝 RECOMMENDATIONS:\n"
                            f"• Minimum 50 characters\n"
                            f"• Include a clear call-to-action\n"
                            f"• Add unsubscribe instructions\n\n"
                            f"🔔 You'll receive Telegram report with:\n"
                            f"• Success/failure count\n"
                            f"• Error types breakdown\n"
                            f"• Troubleshooting guide\n"
                            f"• Sample error messages"
                        )
                else:
                    await query.edit_message_text("❌ Failed to set format. Please start over with /send")
    
    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.start_cmd(update, context)
    
    def run(self):
        print("=" * 70)
        print("🔒 ADMIN-CONTROLLED BULK EMAIL BOT (OPTIMIZED VERSION)")
        print(f"👑 Admin User ID: {self.admin_id}")
        print(f"👥 Max users per code: {MAX_USERS_PER_CODE}")
        print(f"⏱️ Email delay: {EMAIL_DELAY} seconds")
        print("📧 Step-by-step: emails → subject → format → body/file")
        print("📢 COMPREHENSIVE ERROR REPORTING TO TELEGRAM: ENABLED")
        print("⚡ PERFORMANCE OPTIMIZATIONS:")
        print("   • LRU caching for frequently accessed data")
        print("   • SMTP connection pooling (reuse connections)")
        print("   • Thread-safe operations with locks")
        print("   • Optimized regex patterns")
        print("   • Batch processing for large lists")
        print("=" * 70)
        
        print(f"\n📝 SPECIFIC CODES MONITORED:")
        print(f"   • OWK475 - Pre-added (inactive by default)")
        print(f"   • All codes saved in access_data.json")
        
        if self.admin_id == 0:
            print("\n⚠️  CRITICAL: You must set your ADMIN_USER_ID!")
            print("1. Message @userinfobot on Telegram")
            print("2. Get your user ID")
            print("3. Replace ADMIN_USER_ID = 0 with your ID")
            print("\nExample: ADMIN_USER_ID = 123456789")
            return
        
        if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
            print("\n❌ Replace 'YOUR_TELEGRAM_BOT_TOKEN_HERE' with your bot token!")
            print("1. Message @BotFather on Telegram")
            print("2. Send /newbot")
            print("3. Copy token and paste in the script")
            return
        
        print(f"\n🤖 Bot is starting...")
        print(f"💡 Admin commands available for user {self.admin_id}")
        self.app.run_polling()

# ===================== MAIN =====================

def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    bot = EmailBot(TELEGRAM_BOT_TOKEN, ADMIN_USER_ID)
    bot.run()

if __name__ == "__main__":
    main()