"""Seven Shanghai calendar dates, using only this store's signed options."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pickup_options as pickup


class SevenCalendarDaysTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,12,30,9,0,tzinfo=ZoneInfo('Asia/Shanghai'))
        clock=patch('pickup_options.pickup_calendar_now',side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        handler=patch('interaction.handler',None);handler.start();self.addCleanup(handler.stop)

    def flow(self,rows):
        dates=[];windows=[]
        for iso,start,end,extra in rows:
            number=str(datetime.fromisoformat(iso).day)
            dates.append({'date':iso,'dayOfMonth':number})
            option={'SlotId':'signed-slot-'+iso+'-'+start,'signKey':'signed-key-'+iso,
                    'timeZone':'Asia/Shanghai','timeSlotValue':number+'-'+start+'-'+end,
                    'checkInStart':start,'checkInEnd':end,'enabled':True,**extra}
            windows.append({number:[option]})
        return SimpleNamespace(model={'slot':{'d':{'storeId':'R001','enableTimeSlotWindows':True},
            'dateTimeSlots':{'d':{'storeSelected':'R001','pickUpDates':dates,'timeSlotWindows':windows}}}})

    def selected(self,flow,**options):
        return pickup.time_slot_payload(flow,'slot','R001',**options)

    def test_tomorrow_is_available_when_today_has_no_valid_slots(self):
        flow=self.flow([('2026-12-30','08:00','08:15',{}),('2026-12-31','10:00','10:15',{})])
        result=self.selected(flow)
        self.assertEqual(result['slot.dateTimeSlots.date'],'2026-12-31')
        self.assertEqual(result['slot.dateTimeSlots.signKey'],'signed-key-2026-12-31')
        self.assertEqual(result['slot.dateTimeSlots.timeSlotId'],'signed-slot-2026-12-31-10:00')

    def test_today_and_sixth_following_date_are_included_but_seventh_is_not(self):
        flow=self.flow([('2027-01-06','08:00','08:15',{}),('2027-01-05','11:00','11:15',{}),
                        ('2026-12-29','12:00','12:15',{})])
        self.assertEqual(self.selected(flow)['slot.dateTimeSlots.date'],'2027-01-05')
        flow=self.flow([('2027-01-06','08:00','08:15',{}),('2026-12-29','12:00','12:15',{})])
        with self.assertRaises(pickup.NoPickupSlot):self.selected(flow)

    def test_earliest_current_day_wins_over_later_dates_and_legacy_rules(self):
        flow=self.flow([('2026-12-31','09:00','09:15',{}),('2026-12-30','11:00','11:15',{}),
                        ('2026-12-30','10:00','10:15',{})])
        before=deepcopy(flow.model)
        for rule in ({'mode':'latest'},{'mode':'window','start':'15:00','end':'18:00','fallback':'stop'}):
            self.assertEqual(self.selected(flow,rule=rule)['slot.dateTimeSlots.timeSlotValue'],'30-10:00-10:15')
        self.assertEqual(flow.model,before)
        choose=Mock(side_effect=AssertionError('Earliest is unified for every purchase entry'))
        self.assertEqual(self.selected(flow,choose_slot=choose)['slot.dateTimeSlots.timeSlotValue'],'30-10:00-10:15')

    def test_disabled_restricted_unsigned_or_unknown_timezone_slots_are_not_candidates(self):
        bad=[{'enabled':False},{'disabled':True},{'isRestricted':True},{'signKey':''},
             {'SlotId':''},{'timeZone':'Unknown/Invalid'}]
        for extra in bad:
            with self.subTest(extra=list(extra)):
                flow=self.flow([('2026-12-30','10:00','10:15',extra),('2026-12-31','11:00','11:15',{})])
                self.assertEqual(self.selected(flow)['slot.dateTimeSlots.date'],'2026-12-31')

    def test_shanghai_midnight_and_official_timezone_are_used_instead_of_server_local_date(self):
        self.now=datetime(2026,12,31,0,30,tzinfo=ZoneInfo('Asia/Shanghai'))
        flow=self.flow([('2026-12-30','17:00','17:15',{'timeZone':'UTC'}),
                        ('2026-12-30','10:00','10:15',{})])
        result=self.selected(flow)
        self.assertEqual(result['slot.dateTimeSlots.date'],'2026-12-30')
        self.assertEqual(result['slot.dateTimeSlots.timeZone'],'UTC')
        self.assertEqual(result['slot.dateTimeSlots.timeSlotValue'],'30-17:00-17:15')

    def test_window_ending_now_is_expired_but_current_bookable_window_is_kept(self):
        flow=self.flow([('2026-12-30','08:45','09:00',{}),('2026-12-30','08:55','09:10',{}),
                        ('2026-12-30','10:00','10:15',{})])
        self.assertEqual(self.selected(flow)['slot.dateTimeSlots.timeSlotValue'],'30-08:55-09:10')

    def test_store_signature_date_mismatch_is_rejected(self):
        flow=self.flow([('2026-12-31','10:00','10:15',{})])
        flow.model['slot']['dateTimeSlots']['d']['storeSelected']='R002'
        with self.assertRaises(ValueError):self.selected(flow)
        flow.model['slot']['dateTimeSlots']['d']['storeSelected']='R001'
        flow.model['slot']['dateTimeSlots']['d']['pickUpDates'][0]['dayOfMonth']='30'
        with self.assertRaises(pickup.NoPickupSlot):self.selected(flow)
