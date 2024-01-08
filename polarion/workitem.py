import copy
import os
from datetime import datetime, date
from enum import Enum
from collections import namedtuple
from typing import Iterable

from zeep import xsd

from .test_table import TestTable
from .base.comments import Comments
from .base.custom_fields import CustomFields, PolarionAccessError, PolarionWorkitemAttributeError
from .factory import Creator
from .user import User

LinkedWorkitem = namedtuple('LinkedWorkitem', ['role', 'uri'])


class Workitem(CustomFields, Comments):
    """
    Create a Polarion workitem object from the following parameters:
        - polarion client and existing workitem uri
        - polarion client, Project and existing workitem id
        - polarion client, Project and new_workitem_type. This creates a new workitem. The new_workitem_fields must
            contain all the required fields for the new workitem type. If required fields are missing, an exception will
            be raised identifying the missing fields.

    :param polarion: Polarion client object
    :param project: Polarion Project object
    :param id: Workitem ID
    :param uri: Polarion uri
    :param polarion_workitem: Polarion workitem content
    """

    class HyperlinkRoles(Enum):
        """
        Hyperlink reference type enum
        """
        INTERNAL_REF = 'internal reference'
        EXTERNAL_REF = 'external reference'

    def __init__(self, polarion, project=None, id=None, uri=None, new_workitem_type=None, new_workitem_fields=None,
                 polarion_workitem=None):

        super().__init__(polarion, project, id, uri)
        self._polarion = polarion
        self._project = project
        # self._id = id  # This is already done by the super class
        # self._uri = uri  #  This is already done by the super class
        self._postpone_save = False
        self._legacy_test_steps_table = None  # Kept to support legacy code : addTestStep, removeTestStep,
        # updateTestStep, etc...

        service = self._polarion.getService('Tracker')

        if self._uri:
            try:
                self._polarion_item = service.getWorkItemByUri(self._uri)
            except Exception as err:
                raise PolarionAccessError(
                    f'Cannot load workitem {self._uri} within Polarion server {self._polarion.polarion_url}\n'
                    f'This exception was raised: {err}')

            self._id = self._polarion_item.id
        elif id is not None:
            if self._project is None:
                raise PolarionAccessError(f'Provide a project when creating a workitem from an id')
            try:
                self._polarion_item = service.getWorkItemById(
                    self._project.id, self.id)
            except Exception as e:
                raise PolarionAccessError(
                    f'Error loading workitem "{self.id}" in project "{self._project.id}"'
                    f' on server "{self._polarion.polarion_url}":\n{e}')
        elif new_workitem_type is not None:
            if self._project is None:
                raise PolarionAccessError(f'Provide a project when creating a workitem from an id')
            # construct empty workitem
            self._polarion_item = self._polarion.WorkItemType(
                type=self._polarion.EnumOptionIdType(id=new_workitem_type))
            self._polarion_item.project = self._project.polarion_data

            # get the required field for a new item
            required_features = service.getInitialWorkflowActionForProjectAndType(self._project.id, self._polarion.EnumOptionIdType(id=new_workitem_type))
            if required_features.requiredFeatures is not None:
                # if there are any, go and check if they are all supplied
                if new_workitem_fields is None or not set(required_features.requiredFeatures.item) <= new_workitem_fields.keys():
                    # let the user know with a better error
                    raise PolarionWorkitemAttributeError(f'New workitem required field: {required_features.requiredFeatures.item} to be filled in using new_workitem_fields')

            if new_workitem_fields is not None:
                for new_field in new_workitem_fields:
                    if new_field in self._polarion_item:
                        self._polarion_item[new_field] = new_workitem_fields[new_field]
                    else:
                        raise PolarionWorkitemAttributeError(f'{new_field} in new_workitem_fields is not recognised as a workitem field')

            # and create it
            new_uri = service.createWorkItem(self._polarion_item)
            # reload from polarion
            self._polarion_item = service.getWorkItemByUri(new_uri)
            self._id = self._polarion_item.id

        elif polarion_workitem is not None:
            self._polarion_item = polarion_workitem
            self._id = self._polarion_item.id
        else:
            raise PolarionAccessError('No id, uri, polarion workitem or new workitem type specified!')

        if self._project is None:  # If this is not given
            # get the project from the uri
            polarion_project_id = self._polarion_item.project.id
            self._project = polarion.getProject(polarion_project_id)

        self._buildWorkitemFromPolarion()

    @property
    def url(self):
        """
        Get the url for the workitem

        :return: The url
        :rtype: str
        """
        return f'{self._polarion.polarion_url}/#/project/{self._project.id}/workitem?id={self.id}'

    @property
    def document(self):
        location_split = self.location.split('/')
        try:
            start = location_split.index('modules')
            stop = location_split.index('workitems')
        except ValueError:
            return None
        return '/'.join(location_split[start+1:stop])

    def __enter__(self):
        self._postpone_save = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._postpone_save = False
        self.save()

    def _buildWorkitemFromPolarion(self):
        if self._polarion_item is not None and not self._polarion_item.unresolvable:
            self._original_polarion = copy.deepcopy(self._polarion_item)  # Refreshes the cache
            if self._postpone_save is False:  # This will avoid that the data set be lost.
                for attr, value in self._polarion_item.__dict__.items():
                    for key in value:
                        setattr(self, key, value[key])
        else:
            raise PolarionAccessError(f'Workitem "{self._id}" not retrieved from Polarion'
                                      f' {self._polarion.polarion_url}')

    def getAuthor(self):
        """
        Get the author of the workitem

        :return: Author
        :rtype: User
        """
        if self.author is not None:
            return User(self._polarion, self.author)
        return None

    def removeApprovee(self, user: User):
        """
        Remove a user from the approvers

        :param user: The user object to remove
        """
        service = self._polarion.getService('Tracker')
        service.removeApprovee(self.uri, user.id)
        self._reloadFromPolarion()

    def addApprovee(self, user: User, remove_others=False):
        """
        Adds a user as approvee

        :param user: The user object to add
        :param remove_others: Set to True to make the new user the only approver user.
        """
        service = self._polarion.getService('Tracker')

        if remove_others:
            current_users = self.getApproverUsers()
            for current_user in current_users:
                service.removeApprovee(self.uri, current_user.id)

        service.addApprovee(self.uri, user.id)
        self._reloadFromPolarion()

    def getApproverUsers(self):
        """
        Get an array of approval users

        :return: An array of User objects
        :rtype: User[]
        """
        assigned_users = []
        if self.approvals is not None:
            for approval in self.approvals.Approval:
                assigned_users.append(User(self._polarion, approval.user))
        return assigned_users

    def getAssignedUsers(self):
        """
        Get an array of assigned users

        :return: An array of User objects
        :rtype: User[]
        """
        assigned_users = []
        if self.assignee is not None:
            for user in self.assignee.User:
                if user is not None and user.unresolvable is False:
                    assigned_users.append(User(self._polarion, user))
        return assigned_users

    def removeAssignee(self, user: User):
        """
        Remove a user from the assignees

        :param user: The user object to remove
        """
        service = self._polarion.getService('Tracker')
        service.removeAssignee(self.uri, user.id)
        self._reloadFromPolarion()

    def addAssignee(self, user: User, remove_others=False):
        """
        Adds a user as assignee

        :param user: The user object to add
        :param remove_others: Set to True to make the new user the only assigned user.
        """
        service = self._polarion.getService('Tracker')

        if remove_others:
            current_users = self.getAssignedUsers()
            for current_user in current_users:
                service.removeAssignee(self.uri, current_user.id)

        service.addAssignee(self.uri, user.id)
        self._reloadFromPolarion()

    def getStatusEnum(self):
        """
        tries to get the status enum of this workitem type
        When it fails to get it, the list will be empty

        :return: An array of strings of the statusses
        :rtype: string[]
        """
        try:
            enum = self._project.getEnum(f'{self.type.id}-status')
            return enum
        except Exception:
            return []

    def getResolutionEnum(self):
        """
        tries to get the resolution enum of this workitem type
        When it fails to get it, the list will be empty

        :return: An array of strings of the resolutions
        :rtype: string[]
        """
        try:
            enum = self._project.getEnum(f'{self.type.id}-resolution')
            return enum
        except Exception:
            return []

    def getSeverityEnum(self):
        """
        tries to get the severity enum of this workitem type
        When it fails to get it, the list will be empty

        :return: An array of strings of the severities
        :rtype: string[]
        """
        try:
            enum = self._project.getEnum(f'{self.type.id}-severity')
            return enum
        except Exception:
            return []

    def getAllowedCustomKeys(self):
        """
        Gets the list of keys that the workitem is allowed to have.

        :return: An array of strings of the keys
        :rtype: string[]
        """
        try:
            service = self._polarion.getService('Tracker')
            return service.getCustomFieldKeys(self.uri)
        except Exception:
            return []

    def isCustomFieldAllowed(self, key):
        """
        Checks if the custom field of a given key is allowed.

        :return: If the field is allowed
        :rtype: bool
        """
        return key in self.getAllowedCustomKeys()

    def getAvailableStatus(self):
        """
        Get all available status option for this workitem

        :return: An array of string of the statusses
        :rtype: string[]
        """
        available_status = []
        service = self._polarion.getService('Tracker')
        av_status = service.getAvailableEnumOptionIdsForId(self.uri, 'status')
        for status in av_status:
            available_status.append(status.id)
        return available_status

    def getAvailableActionsDetails(self):
        """
        Get all actions option for this workitem with details

        :return: An array of dictionaries of the actions
        :rtype: dict[]
        """
        available_actions = []
        service = self._polarion.getService('Tracker')
        av_actions = service.getAvailableActions(self.uri)
        for action in av_actions:
            available_actions.append(action)
        return available_actions

    def getAvailableActions(self):
        """
        Get all actions option for this workitem without details

        :return: An array of strings of the actions
        :rtype: string[]
        """
        available_actions = []
        service = self._polarion.getService('Tracker')
        av_actions = service.getAvailableActions(self.uri)
        for action in av_actions:
            available_actions.append(action.nativeActionId)
        return available_actions

    def performAction(self, action_name):
        """
        Perform selected action. An exception will be thrown if some prerequisite is not set.

        :param action_name: string containing the action name
        """
        # get id from action name
        service = self._polarion.getService('Tracker')
        av_actions = service.getAvailableActions(self.uri)
        for action in av_actions:
            if action.nativeActionId == action_name or action.actionName == action_name:
                service.performWorkflowAction(self.uri, action.actionId)

    def performActionId(self, actionId: int):
        """
        Perform selected action. An exception will be thrown if some prerequisite is not set.

        :param actionId: number for the action to perform
        """
        service = self._polarion.getService('Tracker')
        service.performWorkflowAction(self.uri, actionId)

    def setStatus(self, status):
        """
        Sets the status opf the workitem and saves the workitem, not respecting any project configured limits or requirements.

        :param status: name of the status
        """
        if status in self.getAvailableStatus():
            self.status.id = status
            self.save()

    def getStatusId(self):
        return self.status.id

    def getTypeId(self):
        """Returns the type qualifier"""
        return self.type.id

    # TODO: Implement a getTypeDescription that returns the User Interface name of the Type

    def getTitle(self):
        """
        :returns the title of the workitem
        :rtype: str"""
        return self.title

    def getDescription(self):
        """
        Get a comment if available. The comment may contain HTML if edited in Polarion!

        :return: The content of the description, may contain HTML
        :rtype: string
        """
        if self.description is not None:
            return self.description.content
        return None

    def setDescription(self, description):
        """
        Sets the description and saves the workitem

        :param description: the description
        """
        self.description = self._polarion.TextType(
            content=description, type='text/html', contentLossy=False)
        self.save()

    def setResolution(self, resolution):
        """
        Sets the resolution and saves the workitem

        :param resolution: the resolution
        """

        if self.resolution is not None:
            self.resolution.id = resolution
        else:
            self.resolution = self._polarion.EnumOptionIdType(
                id=resolution)
        self.save()

    def hasTestSteps(self):
        """
        Checks if the workitem has test steps

        :return: True/False
        :rtype: boolean
        """
        return self._hasTestStepField()

    def getTestTable(self, clear_table=False) -> TestTable:
        """
        Returns an object containing the test steps. This object can be
        used to add, remove, insert and append test steps. Then, the setTestSteps()
        method can be used to commit the test steps back to Polarion.

        :param clear_table: Clears all the test-steps after copying, only keeping the 
            template. This is useful to rewrite a complete test sequence.
        :return: An Object containing the test table
        :rtype: TestTable
        """
        if self._hasTestStepField():
            self._legacy_test_steps_table = TestTable(self, clear_table)
            return self._legacy_test_steps_table
        else:
            raise PolarionWorkitemAttributeError('Work item does not have test step custom field')

    def getRawTestSteps(self):
        """
        Get the raw test steps from the workitem. This is the TestStepArray object as returned by the Polarion API.

        :return: The raw test steps
        :rtype: TestStepArray or None
        """
        if self._polarion_item is not None and not self._polarion_item.unresolvable:
            try:
                # get the custom fields
                if self._hasTestStepField():
                    service_test = self._polarion.getService('TestManagement')
                    return service_test.getTestSteps(self.uri)
            except Exception as  e:
                # fail silently as there are probably not test steps for this workitem
                # todo: logging support
                pass
        return None

    def setTestSteps(self, test_steps) -> None:
        """
        Sets the test steps and saves the workitem. The TestTable object can be obtained from getTestTable().

        :param test_steps: The TestTable object
        :type test_steps: TestTable or the TestStepArray directly obtained from getRawTestSteps
        :return:
        :rtype:
        """
        if isinstance(test_steps, TestTable):  # if the complete TestTable was passed, use only the needed part
            test_steps = test_steps.steps

        assert hasattr(test_steps, 'TestStep')
        assert len(test_steps.TestStep) > 0

        if self._hasTestStepField():
            columns = self.getTestStepHeaderID()
            # Sanity Checks here
            # 1. The format is as expected
            for step in test_steps.TestStep:
                assert len(step.values.Text) == len(columns)
                for col in range(len(columns)):
                    if step.values.Text[col].content is not None:
                        assert step.values.Text[col].type == 'text/html' and \
                               isinstance(step.values.Text[col].content, str) and \
                               step.values.Text[col].contentLossy is False
                    else:
                        step.values.Text[col].content = ''  # Get rid of None values. They are not allowed in Polarion,
                        # but polarion converts '' into None, so we need to convert it back

        service_test = self._polarion.getService('TestManagement')
        service_test.setTestSteps(self.uri, test_steps)

    def getTestRuns(self, limit=-1):
        if not self._hasTestStepField():
            return None

        client = self._polarion.getService('TestManagement')
        polarion_test_runs = client.searchTestRunsWithFieldsLimited(self.id, 'Created', ['id'], limit)

        return [test_run.uri for test_run in polarion_test_runs]

    def addHyperlink(self, url, hyperlink_type: HyperlinkRoles):
        """
        Adds a hyperlink to the workitem.

        :param url: The URL to add
        :param hyperlink_type: Select internal or external hyperlink. Can be a string for custom link types.
        """
        service = self._polarion.getService('Tracker')
        if isinstance(hyperlink_type, Enum):  # convert Enum to str
            hyperlink_type = hyperlink_type.value
        service.addHyperlink(self.uri, url, {'id': hyperlink_type})
        self._reloadFromPolarion()

    def removeHyperlink(self, url):
        """
        Removes the url from the workitem
        @param url: url to remove
        @return:
        """
        service = self._polarion.getService('Tracker')
        service.removeHyperlink(self.uri, url)
        self._reloadFromPolarion()

    def addLinkedItem(self, workitem, link_type):
        """
            Add a link to a workitem

            :param workitem: A workitem
            :param link_type: The link type
        """

        service = self._polarion.getService('Tracker')
        service.addLinkedItem(self.uri, workitem.uri, role={'id': link_type})
        self._reloadFromPolarion()
        workitem._reloadFromPolarion()

    def removeLinkedItem(self, workitem, role=None):
        """
        Remove the workitem from the linked items list. If the role is specified, the specified link will be removed.
        If not specified, all links with the workitem will be removed

        :param workitem: Workitem to be removed
        :param role: the role to remove
        :return: None
        """
        service = self._polarion.getService('Tracker')
        if role is not None:
            service.removeLinkedItem(self.uri, workitem.uri, role={'id': role})
        else:
            if self.linkedWorkItems is not None:
                for linked_item in self.linkedWorkItems.LinkedWorkItem:
                    if linked_item.workItemURI == workitem.uri:
                        service.removeLinkedItem(self.uri, linked_item.workItemURI, role=linked_item.role)
            if self.linkedWorkItemsDerived is not None:
                for linked_item in self.linkedWorkItemsDerived.LinkedWorkItem:
                    if linked_item.workItemURI == workitem.uri:
                        service.removeLinkedItem(linked_item.workItemURI, self.uri, role=linked_item.role)
        self._reloadFromPolarion()
        workitem._reloadFromPolarion()

    def getLinkedItemWithRoles(self):
        """
        Get linked workitems both linked and back linked item will show up. Will include link roles.

        @return: Array of tuple ('link type', Workitem)
        """
        linked_items = []
        service = self._polarion.getService('Tracker')
        if self.linkedWorkItems is not None:
            for linked_item in self.linkedWorkItems.LinkedWorkItem:
                if linked_item.role is not None:
                    linked_items.append((linked_item.role.id, Workitem(self._polarion, self._project, uri=linked_item.workItemURI)))
        if self.linkedWorkItemsDerived is not None:
            for linked_item in self.linkedWorkItemsDerived.LinkedWorkItem:
                if linked_item.role is not None:
                    linked_items.append((linked_item.role.id, Workitem(self._polarion, self._project, uri=linked_item.workItemURI)))
        return linked_items

    def getLinkedItem(self):
        """
        Get linked workitems both linked and back linked item will show up.

        @return: Array of  Workitem
        @return:
        """
        return [item[1] for item in self.getLinkedItemWithRoles()]

    def hasAttachment(self) -> bool:
        """
        Checks if the workitem has attachments

        :return: True/False
        :rtype: boolean
        """
        if self.attachments is not None:
            return True
        return False

    def getAttachment(self, id) -> bytes:
        """
        Get the attachment data

        :param id: The attachment id
        :return: list of bytes
        :rtype: bytes
        """
        service = self._polarion.getService('Tracker')
        return service.getAttachment(self.uri, id)

    def getAttachments(self) -> list:
        """
        Returns a list of attachments
        :return: list of attachments
        :rtype: list
        """
        if self.attachments is not None:
            return self.attachments.Attachment
        return []

    def getAttachmentInfo(self, attachment_id: str):
        """
        Returns the attachment info for a given attachment_id
        :param attachment_id: Returns the dictionary with the attachment info
        :type attachment_id: str
        :return: AttachmentInfo
        :rtype: AttachmentInfo or None
        """
        if self.hasAttachment():
            for attachment in self.getAttachments():
                if attachment.id == attachment_id:
                    return attachment

    def saveAttachmentAsFile(self, id, file_path):
        """
        Save an attachment to file.

        :param id: The attachment id
        :param file_path: File where to save the attachment
        """
        bin = self.getAttachment(id)
        with open(file_path, "wb") as file:
            file.write(bin)

    def deleteAttachment(self, id):
        """
        Delete an attachment.

        :param id: The attachment id
        """
        service = self._polarion.getService('Tracker')
        service.deleteAttachment(self.uri, id)
        self._reloadFromPolarion()

    def addAttachment(self, file_path, title):
        """
        Upload an attachment

        :param file_path: Source file to upload
        :param title: The title of the attachment
        """
        service = self._polarion.getService('Tracker')
        file_name = os.path.split(file_path)[1]
        with open(file_path, "rb") as file_content:
            service.createAttachment(self.uri, file_name, title, file_content.read())
        self._reloadFromPolarion()

    def addAttachmentData(self, data, title, file_name):
        """
        Upload an attachment

        :param id: The attachment id
        :param data: binary data of the attachment
        :param title: The title of the attachment
        :param file_name: The name of the file
        """
        service = self._polarion.getService('Tracker')
        service.createAttachment(self.uri, file_name, title, data)
        self._reloadFromPolarion()

    def updateAttachment(self, id, file_path, title):
        """
        Upload an attachment

        :param id: The attachment id
        :param file_path: Source file to upload
        :param title: The title of the attachment
        """
        service = self._polarion.getService('Tracker')
        file_name = os.path.split(file_path)[1]
        with open(file_path, "rb") as file_content:
            service.updateAttachment(self.uri, id, file_name, title, file_content.read())
        self._reloadFromPolarion()

    def updateAttachmentData(self, id, data, title, file_name) -> None:
        """
        Upload an attachment

        :param id: The attachment id
        :type id: str
        :param data: Data to upload
        :type data: bytes
        :param file_name: The name of the file
        :type file_name: str
        :param title: The title of the attachment
        :type title: str
        """
        service = self._polarion.getService('Tracker')
        service.updateAttachment(self.uri, id, file_name, title, data)
        self._reloadFromPolarion()

    def getProject(self):
        """
        Get the project object

        :return: Project object
        :rtype: Project
        """
        return self._polarion.getProject(self.project.id)

    def getDocument(self):
        """
        Get the document object

        :return: Document object
        :rtype: Document
        """
        if self.document is not None:
            proj = self.getProject()
            return proj.getDocument(self.document)
        return None

    def delete(self):
        """
        Delete the work item in polarion
        This does not remove workitem references from documents
        :return: Nothing
        :rtype: None
        """
        service = self._polarion.getService('Tracker')
        service.deleteWorkItem(self.uri)

    def moveToDocument(self, document, parent, order=-1):
        """
        Move the work item into a document as a child of another workitem

        :param document: Target document
        :param parent: Parent workitem, None if it shall be placed as top item
        :param order: Order of the workitem, -1 for last
        :type order: int
        """
        service = self._polarion.getService('Tracker')
        service.moveWorkItemToDocument(self.uri, document.uri, parent.uri if parent is not None else xsd.const.Nil,
                                       order, False)

    def addTestStep(self, *args):
        """
        Add a new test step to a test case work item
        @param args: list of strings, one for each column
        @return: None
        """
        if self._hasTestStepField() is False:
            raise PolarionWorkitemAttributeError('Cannot add test steps to work item that does not have the custom field')
        if self._legacy_test_steps_table is None:
            self._legacy_test_steps_table = TestTable(self)
        self._legacy_test_steps_table.addTestStep(*args)
        self.setTestSteps(self._legacy_test_steps_table)

    def removeTestStep(self, index: int):
        """
        Remove a test step at the specified index.
        @param index: zero based index
        @return: None
        """
        if self._hasTestStepField() is False:
            raise PolarionWorkitemAttributeError('Cannot remove test steps to work item that does not have the custom field')
        if self._legacy_test_steps_table is None:
            self._legacy_test_steps_table = TestTable(self)
        self._legacy_test_steps_table.removeTestStep(index)
        self.setTestSteps(self._legacy_test_steps_table)

    def updateTestStep(self, index: int, *args):
        """
        Update a test step at the specified index.
        @param index: zero based index
        @param args: list of strings, one for each column
        @return: None
        """
        if self._hasTestStepField() is False:
            raise PolarionWorkitemAttributeError('Cannot update test steps to work item that does not have the custom field')
        if self._legacy_test_steps_table is None:
            self._legacy_test_steps_table = TestTable(self)
        self._legacy_test_steps_table.updateTestStep(index, *args)
        self.setTestSteps(self._legacy_test_steps_table)

    def getTestStepHeader(self):
        """
        Get the Header names for the test step header.
        @return: List of strings containing the header names.
        """
        # check test step custom field
        if self._hasTestStepField() is False:
            raise PolarionWorkitemAttributeError('Work item does not have test step custom field')

        return self._getConfiguredTestStepColumns()

    def getTestStepHeaderID(self):
        """
        Get the Header ID for the test step header.
        @return: List of strings containing the header IDs.
        """
        if self._hasTestStepField() is False:
            raise PolarionWorkitemAttributeError('Work item does not have test step custom field')

        return self._getConfiguredTestStepColumnIDs()

    def getTestSteps(self):
        """
        Return a list of test steps.
        @return: Array of test steps
        """
        if self._hasTestStepField() is False:
            return []
        if self._legacy_test_steps_table is None:
            self._legacy_test_steps_table = TestTable(self)
        return self._legacy_test_steps_table

    def getLastRevisionNumber(self) -> int:
        """
        Return the revision number of the work item.
        It stores the number in the object for later use.
        @return: Integer with revision number
        """
        if hasattr(self, 'revision_number'):
            return self.revision_number

        service = self._polarion.getService('Tracker')
        try:
            history: list = service.getRevisions(self.uri)
            self.revision_number = int(history[-1])
            return self.revision_number
        except:
            raise PolarionWorkitemAttributeError("Could not get Revision!")

    def _getConfiguredTestStepColumns(self):
        """
        Return a list of coulmn headers
        @return: [str]
        """
        columns = []
        service = self._polarion.getService('TestManagement')
        config = service.getTestStepsConfiguration(self._project.id)
        for col in config:
            columns.append(col.name)
        return columns

    def _getConfiguredTestStepColumnIDs(self):
        """
        Return a list of column header IDs.
        @return: [str]
        """
        columns = []
        service = self._polarion.getService('TestManagement')
        config = service.getTestStepsConfiguration(self._project.id)
        for col in config:
            columns.append(col.id)
        return columns

    def _testStepNoneCheck(self):
        """
        Sanity check on content of test steps when empty strings are use.
        Sometimes they show up as None, which is not accepted by the API.
        @return: None
        """
        for step_id, step in enumerate(self._polarion_test_steps.steps.TestStep):
            for col_id, col in enumerate(self._polarion_test_steps.steps.TestStep[step_id].values.Text):
                if self._polarion_test_steps.steps.TestStep[step_id].values.Text[col_id].content is None:
                    self._polarion_test_steps.steps.TestStep[step_id].values.Text[col_id].content = ""

    def _hasTestStepField(self):
        """
        Checks if the testSteps custom field is available for this workitem. If so it allows test steps to be added.
        @return: True when test steps are available
        """
        service = self._polarion.getService('Tracker')
        custom_fields = service.getCustomFieldKeys(self.uri)
        if 'testSteps' in custom_fields:
            return True
        return False

    def save(self):
        """
        Update the workitem in polarion
        """
        if self._postpone_save:
            return
        updated_item = {}

        for attr, value in self._polarion_item.__dict__.items():
            for key in value:
                current_value = getattr(self, key)
                prev_value = getattr(self._original_polarion, key)
                if current_value != prev_value:
                    updated_item[key] = current_value
        if len(updated_item) > 0:
            updated_item['uri'] = self.uri
            service = self._polarion.getService('Tracker')
            service.updateWorkItem(updated_item)
            self._reloadFromPolarion()

    @property
    def postpone_save(self):
        return self._postpone_save

    @postpone_save.setter
    def postpone_save(self, value):
        self._postpone_save = value
        if value is False:
            self.save()

    def revert_changes(self):
        """Cancels the changes made to the workitem and reloads the data from Polarion"""
        self._postpone_save = False
        self._reloadFromPolarion()

    def getLastFinalized(self):
        if hasattr(self, 'lastFinalized'):
            return self.lastFinalized

        try:
            history = self._polarion.generateHistory(self.uri, ignored_fields=[f for f in dir(self._polarion_item) if f not in ['status']])

            for h in history[::-1]:
                if h.diffs:
                    for d in h.diffs.item:
                        if d.fieldName == 'status' and d.after.id == 'finalized':
                            self.lastFinalized = h.date
                            return h.date
        except:
            pass

        return None

    class WorkItemIterator:
        """Workitem iterator for linked and backlinked workitems"""

        def __init__(self, polarion, linkedWorkItems, roles: Iterable = None):
            self._polarion = polarion
            self._linkedWorkItems = linkedWorkItems
            self._index = 0
            self._disallowed_roles = None
            self._allowed_roles = None
            if roles is not None:
                roles = (roles,) if isinstance(roles, str) else roles
                for role in roles:
                    if role.startswith('~'):
                        if self._disallowed_roles is None:
                            self._disallowed_roles = []
                        self._disallowed_roles.append(role[1:])
                    else:
                        if self._allowed_roles is None:
                            self._allowed_roles = []
                        self._allowed_roles.append(role)

        def __iter__(self):
            return self

        def __next__(self) -> LinkedWorkitem:
            if self._linkedWorkItems is None:
                raise StopIteration
            try:
                while True:
                    if self._index < len(self._linkedWorkItems.LinkedWorkItem):
                        obj = self._linkedWorkItems.LinkedWorkItem[self._index]
                        self._index += 1

                        try:
                            role = obj.role.id
                        except AttributeError:
                            role = 'NA'

                        uri = obj.workItemURI

                        if (self._disallowed_roles is None or role not in self._disallowed_roles) and \
                           (self._allowed_roles is None or role in self._allowed_roles):
                            return LinkedWorkitem(role, uri)
                    else:
                        raise StopIteration

            except IndexError:
                raise StopIteration
            except AttributeError:
                raise StopIteration

    def iterateLinkedWorkItems(self, roles: Iterable = None) -> WorkItemIterator:
        return Workitem.WorkItemIterator(self._polarion, self._polarion_item.linkedWorkItems, roles=roles)

    def iterateLinkedWorkItemsDerived(self, roles: Iterable = None) -> WorkItemIterator:
        return Workitem.WorkItemIterator(self._polarion, self._polarion_item.linkedWorkItemsDerived, roles=roles)

    def _reloadFromPolarion(self):
        service = self._polarion.getService('Tracker')
        self._polarion_item = service.getWorkItemByUri(self._polarion_item.uri)
        self._buildWorkitemFromPolarion()
        # deepcopy was removed from here because it was already being done in
        # _buildWorkitemFromPolarion() method

    def __eq__(self, other):
        try:
            a = vars(self)
            b = vars(other)
        except Exception:
            return False
        return self._compareType(a, b)

    def __hash__(self):
        return self.id

    def _compareType(self, a, b):
        basic_types = [int, float,
                       bool, type(None), str, datetime, date]

        for key in a:
            if key.startswith('_'):
                # skip private types
                continue
            # first to a quick type compare to catch any easy differences
            if type(a[key]) == type(b[key]):
                if type(a[key]) in basic_types:
                    # direct compare capable
                    if a[key] != b[key]:
                        return False
                elif isinstance(a[key], list):
                    # special case for list items
                    if len(a[key]) != len(b[key]):
                        return False
                    for idx, sub_a in enumerate(a[key]):
                        self._compareType(sub_a, b[key][idx])
                else:
                    if not self._compareType(a[key], b[key]):
                        return False
            else:
                # exit, type mismatch
                return False
        # survived all exits, must be good then
        return True

    def __repr__(self):
        return f'{self._id}: {self._polarion_item.title}'

    def __str__(self):
        return f'{self._id}: {self.title}'


class WorkitemCreator(Creator):
    def createFromUri(self, polarion, project, uri):
        return Workitem(polarion, project, None, uri)
