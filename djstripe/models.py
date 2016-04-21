# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import datetime
import decimal
import json
import traceback as exception_traceback
import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.core.mail import EmailMessage
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import python_2_unicode_compatible, smart_text
from model_utils.models import TimeStampedModel
from stripe.error import StripeError, InvalidRequestError

from . import settings as djstripe_settings
from . import webhooks
from .exceptions import SubscriptionCancellationFailure, SubscriptionUpdateFailure, MultipleSubscriptionException
from .managers import CustomerManager, ChargeManager, TransferManager
from .signals import WEBHOOK_SIGNALS
from .signals import webhook_processing_error
from .stripe_objects import (StripeSource, StripeCharge, StripeCustomer, StripeCard, StripeSubscription,
                             StripePlan, StripeInvoice, StripeTransfer, StripeAccount, StripeEvent)
from .utils import convert_tstamp


logger = logging.getLogger(__name__)


class Charge(StripeCharge):
    account = models.ForeignKey("Account", null=True, related_name="charges", help_text="The account the charge was made on behalf of. Null here indicates that this value was never set.")

    customer = models.ForeignKey("Customer", related_name="charges", help_text="The customer associated with this charge.")
    invoice = models.ForeignKey("Invoice", null=True, related_name="charges", help_text="The invoice associated with this charge, if it exists.")
    transfer = models.ForeignKey("Transfer", null=True, help_text="The transfer to the destination account (only applicable if the charge was created using the destination parameter).")

    source = models.ForeignKey(StripeSource, null=True, related_name="charges")

    receipt_sent = models.BooleanField(default=False)

    objects = ChargeManager()

    def refund(self, amount=None, reason=None):
        refunded_charge = super(Charge, self).refund(amount, reason)
        return Charge.sync_from_stripe_data(refunded_charge)

    def capture(self):
        captured_charge = super(Charge, self).capture()
        return Charge.sync_from_stripe_data(captured_charge)

    def send_receipt(self):
        if not self.receipt_sent:
            site = Site.objects.get_current()
            protocol = getattr(settings, "DEFAULT_HTTP_PROTOCOL", "http")
            ctx = {
                "charge": self,
                "site": site,
                "protocol": protocol,
            }
            subject = render_to_string("djstripe/email/subject.txt", ctx)
            subject = subject.strip()
            message = render_to_string("djstripe/email/body.txt", ctx)
            num_sent = EmailMessage(
                subject,
                message,
                to=[self.customer.subscriber.email],
                from_email=djstripe_settings.INVOICE_FROM_EMAIL
            ).send()
            self.receipt_sent = num_sent > 0
            self.save()

    def attach_objects_hook(self, cls, data):
        customer = cls.stripe_object_to_customer(target_cls=Customer, data=data)
        if customer:
            self.customer = customer
        else:
            raise ValidationError("A customer was not attached to this charge.")

        invoice = cls.stripe_object_to_invoice(target_cls=Invoice, data=data)
        if invoice:
            self.invoice = invoice

        transfer = cls.stripe_object_to_transfer(target_cls=Transfer, data=data)
        if transfer:
            self.transfer = transfer

        # Set the account on this object.
        destination_account = cls.stripe_object_destination_to_account(target_cls=Account, data=data)
        if destination_account:
            self.account = destination_account
        else:
            self.account = Account.get_default_account()

        # TODO: other sources
        if self.source_type == "card":
            self.source = cls.stripe_object_to_source(target_cls=Card, data=data)


class Customer(StripeCustomer):
    doc = """
    Note: Sources and Subscriptions are attached via a ForeignKey on StripeSource.
          Use ``Customer.sources`` and ``Customer.subscriptions`` to access them.
    """
    __doc__ = getattr(StripeCustomer, "__doc__") + doc

    # account = models.ForeignKey(Account, related_name="customers")

    default_source = models.ForeignKey(StripeSource, null=True, related_name="customers")

    subscriber = models.OneToOneField(getattr(settings, 'DJSTRIPE_SUBSCRIBER_MODEL', settings.AUTH_USER_MODEL), null=True)
    date_purged = models.DateTimeField(null=True, editable=False)

    objects = CustomerManager()

    def str_parts(self):
        return [smart_text(self.subscriber), "email={email}".format(email=self.subscriber.email)] + super(Customer, self).str_parts()

    @classmethod
    def get_or_create(cls, subscriber):
        try:
            return Customer.objects.get(subscriber=subscriber), False
        except Customer.DoesNotExist:
            return cls.create(subscriber), True

    @classmethod
    def create(cls, subscriber):
        trial_days = None
        if djstripe_settings.trial_period_for_subscriber_callback:
            trial_days = djstripe_settings.trial_period_for_subscriber_callback(subscriber)

        stripe_customer = cls._api_create(email=subscriber.email)
        customer = Customer.objects.create(subscriber=subscriber, stripe_id=stripe_customer.id)

        if djstripe_settings.DEFAULT_PLAN and trial_days:
            customer.subscribe(plan=djstripe_settings.DEFAULT_PLAN, trial_days=trial_days)

        return customer

    def purge(self):
        try:
            self._api_delete()
        except InvalidRequestError as exc:
            if str(exc).startswith("No such customer:"):
                # The exception was thrown because the stripe customer was already
                # deleted on the stripe side, ignore the exception
                pass
            else:
                # The exception was raised for another reason, re-raise it
                raise

        self.subscriber = None

        # Remove sources
        self.default_source = None
        for source in self.sources.all():
            source.remove()

        super(Customer, self).purge()
        self.date_purged = timezone.now()
        self.save()

    # TODO: Override Queryset.delete() with a custom manager, since this doesn't get called in bulk deletes (or cascades, but that's another matter)
    def delete(self, using=None, keep_parents=False):
        """
        Overriding the delete method to keep the customer in the records. All identifying information is removed via the purge() method.

        The only way to delete a customer is to use SQL.

        """

        self.purge()

    def _plan_none_check(self, plan):
        if self.subscriptions.count() != 1:
            raise TypeError("plan cannot be None if more than one subscription exists for this customer.")

    def has_active_subscription(self, plan=None):
        """
        Checks to see if this customer has an active subscription to the given plan.

        :param plan: The plan for which to check for an active subscription. If plan is None and
                     there exists only one subscription, this method will check if that subscription
                     is active. Calling this method with no plan and multiple subscriptions will throw
                     an exception.
        :type plan: Plan or string (plan ID)

        :returns: True if there exists an active subscription, False otherwise.
        :throws: TypeError if plan is None and more than one subscription exists for this customer.
        """

        try:
            if plan is None:
                self._plan_none_check(plan)

                return self.subscriptions.first().is_valid()
            else:
                # Convert Plan to stripe_id
                if isinstance(plan, Plan):
                    plan = plan.stripe_id

                return any([subscription.is_valid() for subscription in self.subscriptions.filter(plan__id=plan)])
        except Subscription.DoesNotExist:
            return False

    @property
    def subscription(self):
        subscription_count = self.subscriptions.count()

        if subscription_count == 0:
            return None
        elif subscription_count == 1:
            return self.subscriptions.first()
        else:
            raise MultipleSubscriptionException("This customer has multiple subscriptions. Use Customer.subscriptions to access them.")

    # TODO: Accept a coupon object when coupons are implemented
    def subscribe(self, plan, charge_immediately=True, **kwargs):
        """
        See StripeCustomer.subscribe()

        :param charge_immediately: Whether or not to charge for the subscription upon creation. If False, an invoice will be created at the end of this period.
        :type charge_immediately: boolean
        """

        stripe_subscription = super(Customer, self).subscribe(plan, **kwargs)

        if charge_immediately:
            self.send_invoice()

        return Subscription.sync_from_stripe_data(stripe_subscription)

    def cancel_subscription(self, plan=None, at_period_end=djstripe_settings.CANCELLATION_AT_PERIOD_END):
        """
        Cancels a customer’s subscription.

        :param plan: The plan for which to cancel a subscription. If plan is None and there exists only one subscription, this method will cancel that subscription.
                     Calling this method with no plan and multiple subscriptions will throw an exception.
        :type plan: Plan or string (plan ID)
        :param at_period_end: A flag that if set to true will delay the cancellation of the subscription until the end of the current period.
        :type at_period_end: boolean
        :throws: TypeError if plan is None and more than one subscription exists for this customer.
        :throws: MultipleSubscriptionException if a customer has multiple subscriptions to the same plan.
        :throws: SubscriptionCancellationFailure if the subscription for the given plan could not be cancelled.

        NOTE: If a subscription is cancelled during a trial period, the at_period_end flag will be overridden to False so that the trial ends immediately and the customer's card isn't charged.
        """

        if plan is None:
            self._plan_none_check(plan)

            subscription = self.subscriptions.first()
        else:
            # Convert Plan to stripe_id
            if isinstance(plan, Plan):
                plan = plan.stripe_id

            try:
                subscription = self.subscriptions.get(plan__id=plan)
            except Subscription.DoesNotExist:
                raise SubscriptionCancellationFailure("Customer does not have a subscription.")
            except MultipleObjectsReturned:
                raise MultipleSubscriptionException("Customer has multiple subscriptions to the same plan. In order to cancel these subscriptions, use Subscription.cancel().")

        try:
            # If plan has trial days and customer cancels before trial period ends, then end subscription now, i.e. at_period_end=False
            if subscription.trial_end and subscription.trial_end > timezone.now():
                at_period_end = False
            stripe_subscription = subscription.cancel(at_period_end=at_period_end)
        except InvalidRequestError as exc:
            raise SubscriptionCancellationFailure("Customer's information is not current with Stripe.\n{}".format(str(exc)))

        return Subscription.sync_from_stripe_data(stripe_subscription)

    def can_charge(self):
        """Determines if this customer is able to be charged."""

        return self.has_valid_card() and self.date_purged is None

    def charge(self, amount, currency="usd", send_receipt=None, **kwargs):
        """
        See StripeCustomer.charge()

        :param send_receipt: Whether or not to send a receipt for this charge. If blank, ``DJSTRIPE_SEND_INVOICE_RECEIPT_EMAILS`` is used.
        :type send_receipt: boolean
        """

        if send_receipt is None:
            send_receipt = getattr(settings, 'DJSTRIPE_SEND_INVOICE_RECEIPT_EMAILS', True)

        stripe_charge = super(Customer, self).charge(amount=amount, currency=currency, **kwargs)
        charge = Charge.sync_from_stripe_data(stripe_charge)

        if send_receipt:
            charge.send_receipt()

        return charge

    def send_invoice(self):
        try:
            invoice = Invoice._api_create(customer=self.stripe_id)
            invoice.pay()
            return True
        except InvalidRequestError:
            return False  # There was nothing to invoice

    def retry_unpaid_invoices(self):
        self._sync_invoices()
        for invoice in self.invoices.filter(paid=False, closed=False):
            try:
                invoice.retry()  # Always retry unpaid invoices
            except InvalidRequestError as exc:
                if str(exc) != "Invoice is already paid":
                    raise exc

    def has_valid_card(self):
        return self.default_source is not None

    def add_card(self, source, set_default=True):
        new_stripe_card = super(Customer, self).add_card(source, set_default)
        new_card = Card.sync_from_stripe_data(new_stripe_card)

        # Change the default source
        if set_default:
            self.default_source = new_card
            self.save()

        return new_card

    def attach_objects_hook(self, cls, data):
        # TODO: other sources
        if data["default_source"] == "card":
            self.default_source = cls.stripe_object_default_source_to_source(target_cls=Card, data=data)

    # SYNC methods should be dropped in favor of the master sync infrastructure proposed
    def _sync(self):
        stripe_customer = self.api_retrieve()

        if getattr(stripe_customer, 'deleted', False):
            # Customer was deleted from stripe
            self.purge()

    def _sync_invoices(self, **kwargs):
        stripe_customer = self.api_retrieve()

        for invoice in stripe_customer.invoices(**kwargs).data:
            Invoice.sync_from_stripe_data(invoice, send_receipt=False)

    def _sync_charges(self, **kwargs):
        stripe_customer = self.api_retrieve()

        for charge in stripe_customer.charges(**kwargs).data:
            data = Charge(stripe_id=charge["id"]).api_retrieve(charge["id"])
            Charge.sync_from_stripe_data(data)


class Card(StripeCard):
    # account = models.ForeignKey("Account", null=True, related_name="cards")

    def attach_objects_hook(self, cls, data):
        customer = cls.stripe_object_to_customer(target_cls=Customer, data=data)
        if customer:
            self.customer = customer
        else:
            raise ValidationError("A customer was not attached to this card.")

    def remove(self):
        """Removes a card from this customer's account."""

        try:
            self._api_delete()
        except InvalidRequestError as exc:
            if str(exc).startswith("No such customer:"):
                # The exception was thrown because the stripe customer was already
                # deleted on the stripe side, ignore the exception
                pass

        self.delete()


class Subscription(StripeSubscription):
    # account = models.ForeignKey("Account", related_name="subscriptions")
    customer = models.ForeignKey("Customer", related_name="subscriptions", help_text="The customer associated with this subscription.")

    def is_period_current(self):
        return self.current_period_end > timezone.now()

    def is_status_current(self):
        return self.status in ["trialing", "active"]

    def is_status_temporarily_current(self):
        """
        Status when customer canceled their latest subscription, one that does not prorate,
        and therefore has a temporary active subscription until period end.
        """

        return self.canceled_at and self.start < self.canceled_at and self.cancel_at_period_end

    def is_valid(self):
        if not self.is_status_current():
            return False

        if not self.is_period_current():
            return False

        return True

    def update(self, prorate=djstripe_settings.PRORATION_POLICY, **kwargs):
        """
        See StripeSubscription.update()

        Note: The default value for prorate is overridden by the DJSTRIPE_PRORATION_POLICY setting.
        """

        stripe_subscription = super(Subscription, self).update(prorate, **kwargs)
        return Subscription.sync_from_stripe_data(stripe_subscription)

    def extend(self, delta):
        stripe_subscription = super(Subscription, self).extend(delta)
        return Subscription.sync_from_stripe_data(stripe_subscription)

    def attach_objects_hook(self, cls, data):
        self.customer = cls.stripe_object_to_customer(target_cls=Customer, data=data)
        self.plan = cls.stripe_object_to_plan(target_cls=Plan, data=data)


class Plan(StripePlan):
    # account = models.ForeignKey("Account", related_name="plans")

    @classmethod
    def get_or_create(cls, **kwargs):
        try:
            return Plan.objects.get(stripe_id=kwargs['stripe_id']), False
        except Plan.DoesNotExist:
            return cls.create(**kwargs), True

    @classmethod
    def create(cls, **kwargs):
        # A few minor things are changed in the api-version of the create call
        api_kwargs = dict(kwargs)
        api_kwargs['id'] = api_kwargs['stripe_id']
        del(api_kwargs['stripe_id'])
        api_kwargs['amount'] = int(api_kwargs['amount'] * 100)
        cls._api_create(**api_kwargs)

        plan = Plan.objects.create(**kwargs)

        return plan

    # TODO: Move this type of update to the model's save() method so it happens automatically
    # Also, block other fields from being saved.
    def update_name(self):
        """Update the name of the Plan in Stripe and in the db.

        - Assumes the object being called has the name attribute already
          reset, but has not been saved.
        - Stripe does not allow for update of any other Plan attributes besides
          name.

        """

        p = self.api_retrieve()
        p.name = self.name
        p.save()

        self.save()


class Invoice(StripeInvoice):
    # account = models.ForeignKey("Account", related_name="invoices")
    customer = models.ForeignKey(Customer, related_name="invoices")

    class Meta(object):
        ordering = ["-date"]

    def attach_objects_hook(self, cls, data):
        customer = cls.stripe_object_to_customer(target_cls=Customer, data=data)
        if customer:
            self.customer = customer
        else:
            raise ValidationError("A customer was not attached to this charge.")

    @classmethod
    def sync_from_stripe_data(cls, data, send_receipt=True):
        invoice = super(Invoice, cls).sync_from_stripe_data(data)

        for item in data["lines"].get("data", []):
            period_end = convert_tstamp(item["period"], "end")
            period_start = convert_tstamp(item["period"], "start")
            """
            Period end of invoice is the period end of the latest invoiceitem.
            """
            invoice.period_end = period_end

            if item.get("plan"):
                plan = djstripe_settings.plan_from_stripe_id(item["plan"]["id"])
            else:
                plan = ""

            inv_item, inv_item_created = invoice.items.get_or_create(
                stripe_id=item["id"],
                defaults=dict(
                    amount=(item["amount"] / decimal.Decimal("100")),
                    currency=item["currency"],
                    proration=item["proration"],
                    description=item.get("description") or "",
                    line_type=item["type"],
                    plan=plan,
                    period_start=period_start,
                    period_end=period_end,
                    quantity=item.get("quantity"),
                )
            )
            if not inv_item_created:
                inv_item.amount = (item["amount"] / decimal.Decimal("100"))
                inv_item.currency = item["currency"]
                inv_item.proration = item["proration"]
                inv_item.description = item.get("description") or ""
                inv_item.line_type = item["type"]
                inv_item.plan = plan
                inv_item.period_start = period_start
                inv_item.period_end = period_end
                inv_item.quantity = item.get("quantity")
                inv_item.save()

        # Save invoice period end assignment.
        invoice.save()

        if data.get("charge"):
            stripe_charge = Charge(stripe_id=data["charge"]).api_retrieve()
            charge = Charge.sync_from_stripe_data(stripe_charge)

            if send_receipt:
                charge.send_receipt()
        return invoice


@python_2_unicode_compatible
class InvoiceItem(TimeStampedModel):
    # account = models.ForeignKey(Account, related_name="invoiceitems")

    stripe_id = models.CharField(max_length=50)
    invoice = models.ForeignKey(Invoice, related_name="items")
    amount = models.DecimalField(decimal_places=2, max_digits=7)
    currency = models.CharField(max_length=10)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    proration = models.BooleanField(default=False)
    line_type = models.CharField(max_length=50)
    description = models.CharField(max_length=200, blank=True)
    plan = models.CharField(max_length=100, null=True, blank=True)
    quantity = models.IntegerField(null=True)

    def __str__(self):
        return smart_text("<amount={amount}, plan={plan}, stripe_id={stripe_id}>".format(amount=self.amount, plan=smart_text(self.plan), stripe_id=self.stripe_id))

    def plan_display(self):
        return djstripe_settings.PAYMENTS_PLANS[self.plan]["name"]


class Transfer(StripeTransfer):
    # account = models.ForeignKey("Account", related_name="transfers")

    # DEPRECATED. Why do we need this?
    event = models.ForeignKey("Event", null=True, related_name="transfers")

    objects = TransferManager()

    @classmethod
    def process_transfer(cls, event, stripe_object):
        # TODO: Convert to get_or_create
        try:
            transfer = cls.stripe_objects.get_by_json(stripe_object)
            created = False
        except cls.DoesNotExist:
            transfer = cls._create_from_stripe_object(stripe_object)
            created = True

        transfer.event = event

        if created:
            transfer.save()
            for fee in stripe_object["summary"]["charge_fee_details"]:
                transfer.charge_fee_details.create(
                    amount=fee["amount"] / decimal.Decimal("100"),
                    application=fee.get("application", ""),
                    description=fee.get("description", ""),
                    kind=fee["type"]
                )
        else:
            transfer.status = stripe_object["status"]
            transfer.save()

        if event and event.type == "transfer.updated":
            transfer.update_status()
            transfer.save()


class TransferChargeFee(TimeStampedModel):
    transfer = models.ForeignKey(Transfer, related_name="charge_fee_details")
    amount = models.DecimalField(decimal_places=2, max_digits=7)
    application = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    kind = models.CharField(max_length=150)


class Account(StripeAccount):
    pass


@python_2_unicode_compatible
class EventProcessingException(TimeStampedModel):

    event = models.ForeignKey("Event", null=True)
    data = models.TextField()
    message = models.CharField(max_length=500)
    traceback = models.TextField()

    @classmethod
    def log(cls, data, exception, event):
        cls.objects.create(
            event=event,
            data=data or "",
            message=str(exception),
            traceback=exception_traceback.format_exc()
        )

    def __str__(self):
        return smart_text("<{message}, pk={pk}, Event={event}>".format(message=self.message, pk=self.pk, event=self.event))


class Event(StripeEvent):
    # account = models.ForeignKey(Account, related_name="events")

    customer = models.ForeignKey("Customer", null=True,
                                 help_text="In the event that there is a related customer, this will point to that "
                                           "Customer record")
    valid = models.NullBooleanField(null=True,
                                    help_text="Tri-state bool. Null == validity not yet confirmed. Otherwise, this "
                                              "field indicates that this event was checked via stripe api and found "
                                              "to be either authentic (valid=True) or in-authentic (possibly "
                                              "malicious)")

    processed = models.BooleanField(default=False, help_text="If validity is performed, webhook event processor(s) "
                                                             "may run to take further action on the event. Once these "
                                                             "have run, this is set to True.")

    @property
    def message(self):
        return self.webhook_message if self.valid else None

    def validate(self):
        """
        The original contents of the Event message comes from a POST to the webhook endpoint. This data
        must be confirmed by re-fetching it and comparing the fetched data with the original data. That's what
        this function does.

        This function makes an API call to Stripe to re-download the Event data. It then
        marks this record's valid flag to True or False.
        """
        event = self.api_retrieve()
        validated_message = json.loads(
            json.dumps(
                event.to_dict(),
                sort_keys=True,
            )
        )
        self.valid = self.webhook_message == validated_message
        self.save()

    def process(self):
        """
        Call whatever webhook event handlers have registered for this event, based on event "type" and
        event "sub type"

        See event handlers registered in djstripe.event_handlers module (or handlers registered in djstripe plugins or
        contrib packages)
        """
        if self.valid and not self.processed:
            event_type, event_subtype = self.type.split(".", 1)

            try:
                # TODO: would it make sense to wrap the next 4 lines in a transaction.atomic context? Yes it would,
                # except that some webhook handlers can have side effects outside of our local database, meaning that
                # even if we rollback on our database, some updates may have been sent to Stripe, etc in resposne to
                # webhooks...
                webhooks.call_handlers(self, self.message["data"], event_type, event_subtype)
                self.send_signal()
                self.processed = True
                self.save()
            except StripeError as exc:
                # TODO: What if we caught all exceptions or a broader range of exceptions here? How about DoesNotExist
                # exceptions, for instance? or how about TypeErrors, KeyErrors, ValueErrors, etc?
                EventProcessingException.log(
                    data=exc.http_body,
                    exception=exc,
                    event=self
                )
                webhook_processing_error.send(
                    sender=Event,
                    data=exc.http_body,
                    exception=exc
                )

    def send_signal(self):
        signal = WEBHOOK_SIGNALS.get(self.type)
        if signal:
            return signal.send(sender=Event, event=self)


# Much like registering signal handlers. We import this module so that its registrations get picked up
# the NO QA directive tells flake8 to not complain about the unused import
from . import event_handlers  # NOQA
