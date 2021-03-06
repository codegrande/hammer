import logging

from botocore.exceptions import ClientError
from library.utility import jsonDumps


class IAMOperations:
    @staticmethod
    def update_access_key(iam_client, user_name, key_id, status):
        """
        Changes the status of the specified access key from Active to Inactive, or vice versa.

        :param iam_client: IAM boto3 client
        :param user_name: user name to update access key for
        :param key_id: key Id to update
        :param status: status to assign to the access key ('Active'|'Inactive')

        :return: nothing
        """
        iam_client.update_access_key(
            UserName=user_name,
            AccessKeyId=key_id,
            Status=status
        )

    @classmethod
    def disable_access_key(cls, iam_client, user_name, key_id):
        """
        Make `Inactive` given access key.

        :param iam_client: IAM boto3 client
        :param user_name: user name to disable access key for
        :param key_id: key Id to disable

        :return: nothing
        """
        cls.update_access_key(iam_client, user_name, key_id, "Inactive")

class User(object):
    """
    Basic class for IAM User.
    Encapsulates user access keys and criteria for inactive/stale checking.
    """
    def __init__(self, username,
                 account,
                 now=None,
                 rotation_criteria_days=None,
                 inactive_criteria_days=None):
        """
        :param username: name of IAM user
        :param account: `Account` instance where IAM user is present
        :param now: `datetime` object of current timestamp to compare CreateDate/LastUsedDate with
        :param rotation_criteria_days: `timedelta` object to compare and mark access key as stale (create long time ago)
        :param inactive_criteria_days: `timedelta` object to compare and mark access key as inactive (not used for a long time)
        """
        self.id = username
        self.account = account
        self.now = now
        self.rotation_criteria_days = rotation_criteria_days
        self.inactive_criteria_days = inactive_criteria_days
        self.keys = []

    def __str__(self):
        return f"{self.__class__.__name__}(Name={self.id}, Keys={len(self.keys)})"

    def add_key(self, metadata):
        """
        Create and add new IAM key to list of keys

        :param metadata: access key metadata as AWS returns

        :return: created and added `IAMKey` instance (for further modification)
        """
        key = IAMKey(self, metadata)
        self.keys.append(key)
        return key

    def get_key(self, key_id):
        """
        :return: `IAMKey` by key id
        """
        for key in self.keys:
            if key.id == key_id:
                return key
        return None

    @property
    def stale_keys(self):
        """ :return: list of stale IAMKey instances """
        return [key for key in self.keys if key.stale]

    @property
    def inactive_keys(self):
        """ :return: list of inactive IAMKey instances """
        return [key for key in self.keys if key.inactive]

class IAMKey(object):
    """
    Basic class for IAM access key.
    Encapsulates access key Id, create and last used date.
    """
    def __init__(self, user, metadata):
        """
        :param user: IAM user name
        :param metadata: access key metadata as AWS returns
        """
        self.user = user
        self.metadata = metadata
        logging.debug(f"Evaluating '{user.id}' key\n{jsonDumps(self.metadata)}")
        self.id = self.metadata["AccessKeyId"]
        # 'Active' / 'Inactive'
        self.status = self.metadata["Status"]
        # the date when the access key was created
        self.create_date = self.metadata["CreateDate"]
        # the date when the access key was last used
        self._last_used = None

    def __str__(self):
        return (f"{self.__class__.__name__}("
                f"Id={self.id}, "
                f"Status={self.status}, "
                f"CreateDate={self.create_date}, "
                f"LastUsed={self.last_used}"
                f")")

    @property
    def last_used(self):
        return self._last_used

    @last_used.setter
    def last_used(self, details):
        """
        Set timestamp when access key was last used (if available) or created.

        :param details: `get_access_key_last_used` API response
        """
        logging.debug(f"Checking '{self.user.id}'/'{self.id} key'\n{jsonDumps(details)}")
        if "LastUsedDate" in details["AccessKeyLastUsed"]:
            self._last_used = details["AccessKeyLastUsed"]["LastUsedDate"]
        else:
            logging.debug(f"'{self.user.id}'/'{self.id}' key was not used, using 'CreateDate'")
            self._last_used = self.create_date

    @property
    def stale(self):
        """
        :return: boolean, True - if key is active and was created earlier than rotation criteria
                          False - otherwise
        """
        if self.status == "Inactive":
            return False
        assert self.user.now is not None
        assert self.user.rotation_criteria_days is not None
        return (self.user.now - self.create_date) > self.user.rotation_criteria_days

    @property
    def inactive(self):
        """
        :return: boolean, True - if key is active and was last used earlier than inactive criteria
                          False - otherwise
        """
        if self.status == "Inactive":
            return False
        assert self.user.now is not None
        assert self.last_used is not None
        assert self.user.inactive_criteria_days is not None
        return (self.user.now - self.last_used) > self.user.inactive_criteria_days

    def disable(self):
        """ Make `Inactive` current access key """
        IAMOperations.disable_access_key(self.user.account.client("iam"), self.user.id, self.id)


class IAMKeyChecker(object):
    """
    Basic class for checking IAM access keys in account.
    Encapsulates check settings and discovered access keys grouped by users.
    """
    def __init__(self,
                 account,
                 now=None,
                 rotation_criteria_days=None,
                 inactive_criteria_days=None):
        """
        :param account: `Account` instance with IAM users to check
        :param now: `datetime` object of current timestamp to compare CreateDate/LastUsedDate with
        :param rotation_criteria_days: `timedelta` object to compare and mark access keys as stale (created long time ago)
        :param inactive_criteria_days: `timedelta` object to compare and mark access keys as inactive (not used for a long time)
        """
        self.account = account
        self.now = now
        self.rotation_criteria_days = rotation_criteria_days
        self.inactive_criteria_days = inactive_criteria_days
        self.users = []

    def get_user(self, user_id):
        """
        :return: `User` by user id (name)
        """
        for user in self.users:
            if user.id == user_id:
                return user
        return None

    def check(self, users_to_check=None, last_used_check_enabled=False):
        """
        Walk through IAM users access keys in the account and check them (stale/inactive).
        Group access keys by users.
        Put all gathered users to `self.users`.

        :param users_to_check: list with users to check, if it is not supplied - all users must be checked
        :param last_used_check_enabled: boolean to select if access key last used time must be checked

        :return: boolean. True - if check was successful,
                          False - otherwise
        """
        try:
            # get all users in account
            users = self.account.client("iam").list_users()['Users']
        except ClientError as err:
            if err.response['Error']['Code'] in ["AccessDenied", "UnauthorizedOperation"]:
                logging.error(f"Access denied in {self.account} "
                              f"(iam:{err.operation_name})")
            else:
                logging.exception(f"Failed to list users in {self.account}")
            return False

        logging.debug(f"Evaluating users\n{jsonDumps(users)}")
        for user_response in users:
            username = user_response["UserName"]
            if users_to_check is not None and username not in users_to_check:
                continue

            user = User(username,
                        self.account,
                        self.now,
                        self.rotation_criteria_days, self.inactive_criteria_days)
            self.users.append(user)

            try:
                # get all access keys for user
                access_keys = self.account.client("iam").list_access_keys(UserName=user.id)['AccessKeyMetadata']
            except ClientError as err:
                if err.response['Error']['Code'] in ["AccessDenied", "UnauthorizedOperation"]:
                    logging.error(f"Access denied in {self.account} "
                                  f"(iam:{err.operation_name})")
                else:
                    logging.exception(f"Failed to list access keys for {user.id} in {self.account}")
                return False

            logging.debug(f"Evaluating '{user.id}' access keys\n{jsonDumps(access_keys)}")
            for access_key in access_keys:
                key = user.add_key(access_key)
                if key.status == "Inactive":
                    logging.debug(f"{user.id}/{key.id} key is not active")
                elif not last_used_check_enabled:
                    logging.debug(f"{user.id}/{key.id} last used check disabled")
                else:
                    try:
                        key.last_used = self.account.client("iam").get_access_key_last_used(AccessKeyId=key.id)
                    except ClientError as err:
                        if err.response['Error']['Code'] in ["AccessDenied", "UnauthorizedOperation"]:
                            logging.error(f"Access denied in {self.account} "
                                          f"(iam:{err.operation_name})")
                        else:
                            logging.exception(f"Failed to get access key last used for {user.id}/{key.id} in {self.account}")
                        return False
        return True
