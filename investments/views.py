import xlwt
import urllib
import json

from django.views     import View
from django.http      import JsonResponse, HttpResponse
from django.utils     import timezone
from django.db.models import Sum, Q, Prefetch
from django.db        import transaction, IntegrityError

from users.utils        import user_validator
from investments.utils  import Portfolio
from investments.models import PaybackSchedule, UserDeal, UserPayback
from deals.models       import Deal
from users.models       import User

class InvestmentHistoryView(View):
    @user_validator
    def get(self, request):
        try:
            signed_user = request.user
            PAGE_SIZE   = 10
            offset      = int(request.GET.get('offset', 0))
            limit       = int(request.GET.get('limit', PAGE_SIZE)) + offset
            status      = request.GET.get('status', None)
            search      = request.GET.get('search', None)
            user_deals  = UserDeal.objects.filter(user=signed_user).select_related('deal')
            q           = Q()

            count_by_status = {"all": len(user_deals)}

            for deal_status in Deal.Status.__members__:
                count_by_status[Deal.Status[deal_status]] = len(user_deals.filter(deal__status=Deal.Status[deal_status]))

            if status:
                q &= Q(deal__status=status)

            if search:
                q &= Q(deal__name__contains=search) | Q(deal__id__contains=search)

            investments  = user_deals.filter(q).prefetch_related(
                Prefetch('userpayback_set', to_attr='paybacks'),
                Prefetch('userpayback_set', queryset=UserPayback.objects.filter(state=UserPayback.State.PAID.value), to_attr='paid_paybacks')
                )

            summary = {
                "total"       : sum(investment.amount for investment in investments),
                "paidTotal"   : investments.filter(userpayback__state=UserPayback.State.PAID.value)\
                                .aggregate(paid_total=Sum('userpayback__principal'))['paid_total'],
                "paidInterest": investments.filter(userpayback__state=UserPayback.State.PAID.value)\
                                .aggregate(paid_interest=Sum('userpayback__interest'))['paid_interest']
            }

            items = [
                {
                    "id"          : investment.id,
                    "dealIndex"   : investment.deal.id,
                    "item"        : investment.deal.name,
                    "amount"      : investment.amount,
                    "principal"   : sum(payback.principal for payback in investment.paybacks),
                    "interest"    : sum(payback.interest for payback in investment.paybacks),
                    "date"        : timezone.localtime(investment.created_at).strftime("%y.%m.%d"),
                    "grade"       : Deal.Grade(investment.deal.grade).label,
                    "interestRate": investment.deal.earning_rate,
                    "term"        : investment.deal.repayment_period,
                    "status"      : investment.deal.status,
                    "repayment"   : int((sum(paid_payback.principal for paid_payback in investment.paid_paybacks) / investment.amount) * 100),
                    "cycle"       : len(investment.paid_paybacks),
                    "isCancelable": investment.created_at + timezone.timedelta(days=1) < timezone.now(),
                } for investment in investments.order_by('-created_at')[offset:limit]
            ]
            return JsonResponse({"summary":summary,"count": count_by_status, "items":items}, status=200)

        except ValueError:
            return JsonResponse({"message":'VALUE_ERROR'}, status=400)

class InvestmentPortfolioView(View):
    @user_validator
    def get(self, request):
        user = request.user

        user_deals = list(user.userdeal_set.all().prefetch_related('deal'))

        portfolio = Portfolio()

        for user_deal in user_deals:
            portfolio.sort_deal(user_deal)

        results = {
            'grade'       : portfolio.grade,
            'earningRate' : portfolio.earning_rate,
            'category'    : portfolio.category
        }
        
        return JsonResponse({"results": results}, status=200)

class InvestmentSummaryView(View):
    @user_validator
    def get(self, request):
        user = request.user
        
        user_deals_by_status = {}
        for deal_status in Deal.Status.__members__:
            user_deals_by_status[deal_status] = user.userdeal_set.filter(
                Q(deal__status = Deal.Status[deal_status])
            ).prefetch_related(
                Prefetch(
                    'userpayback_set',
                    queryset = UserPayback.objects.all(),
                    to_attr  = 'all_paybacks'
                ),
                Prefetch(
                    'userpayback_set',
                    queryset = UserPayback.objects.filter(state=UserPayback.State.PAID.value),
                    to_attr  = 'paid_paybacks'
                ),
            )

        user_deals_by_status_sums = {}
        for key, filtered_user_deals in user_deals_by_status.items():
            user_deals_by_status_sums[key] = {
                'total_amount'     : int(filtered_user_deals.aggregate(Sum('amount'))['amount__sum'] or 0),
                'total_interest'   : sum(sum(payback.interest for payback in user_deal.all_paybacks) for user_deal in filtered_user_deals),
                'total_commission' : sum(sum(payback.commission for payback in user_deal.all_paybacks) for user_deal in filtered_user_deals),
                'paid_principal'   : sum(sum(payback.principal for payback in user_deal.paid_paybacks) for user_deal in filtered_user_deals),
                'paid_interest'    : sum(sum(payback.interest for payback in user_deal.paid_paybacks) for user_deal in filtered_user_deals),
                'paid_commission'  : sum(sum(payback.commission for payback in user_deal.paid_paybacks) for user_deal in filtered_user_deals)
            }

        applying_invest_amount   = user_deals_by_status_sums['APPLYING']['total_amount'] - \
                                    user_deals_by_status_sums['APPLYING']['paid_principal']
        normal_invest_amount     = user_deals_by_status_sums['NORMAL']['total_amount'] - \
                                    user_deals_by_status_sums['NORMAL']['paid_principal']
        delay_invest_amount      = user_deals_by_status_sums['DELAY']['total_amount'] - \
                                    user_deals_by_status_sums['DELAY']['paid_principal']
        overdue_invest_amount    = user_deals_by_status_sums['OVERDUE']['total_amount'] - \
                                    user_deals_by_status_sums['OVERDUE']['paid_principal']
        nonperform_invest_amount = user_deals_by_status_sums['NONPERFORM']['total_amount'] - \
                                    user_deals_by_status_sums['NONPERFORM']['paid_principal']
        loss_amount              = user_deals_by_status_sums['NONPERFORM_COMPLETION']['total_amount'] - \
                                    user_deals_by_status_sums['NONPERFORM_COMPLETION']['paid_principal']

        invested_amount = sum(value['total_amount'] for value in user_deals_by_status_sums.values())
        complete_amount = sum(value['paid_principal'] for value in user_deals_by_status_sums.values())
        invest_amount   = applying_invest_amount + normal_invest_amount + delay_invest_amount + \
                            overdue_invest_amount + nonperform_invest_amount

        paid_revenue = sum(value['paid_interest'] for value in user_deals_by_status_sums.values()) - \
                        sum(value['paid_commission'] for value in user_deals_by_status_sums.values())

        total_revenue = sum(value['total_interest'] for value in user_deals_by_status_sums.values()) - \
                        sum(value['total_commission'] for value in user_deals_by_status_sums.values())
        
        mortgage_deals = user.userdeal_set.filter(
            Q(deal__category=Deal.Category.MORTGAGE.value)
        ).prefetch_related(
            Prefetch('userpayback_set', queryset=UserPayback.objects.filter(~Q(state=UserPayback.State.PAID.value)), to_attr='left_paybacks')
        )
        
        invest_mortgage_amount = sum(sum(payback.principal for payback in mortgage_deal.left_paybacks) for mortgage_deal in mortgage_deals)

        deposit = {
            'bank'    : user.deposit_bank.name,
            'account' : user.deposit_account,
            'balance' : user.deposit_amount
        }

        invest_limit = {
            'total'        : user.net_invest_limit,
            'remainTotal'  : user.net_invest_limit - invest_amount,
            'remainEstate' : user.net_mortgage_invest_limit - invest_mortgage_amount 
        }

        overview = {
            'earningRate' : round((total_revenue - loss_amount) / complete_amount * 100, 2),
            'asset'       : user.deposit_amount + invest_amount,
            'paidRevenue' : paid_revenue
        }

        invest_status = {
            'totalInvest' : invested_amount,
            'complete'    : complete_amount,
            'delay'       : delay_invest_amount,
            'invest'      : invest_amount,
            'loss'        : loss_amount,
            'normal'      : normal_invest_amount + applying_invest_amount,
            'overdue'     : overdue_invest_amount,
            'nonperform'  : nonperform_invest_amount
        }

        results = {
            'deposit'      : deposit,
            'investLimit'  : invest_limit,
            'overview'     : overview,
            'investStatus' : invest_status
        }

        return JsonResponse({"results": results}, status=200)
        
class XlsxExportView(View):
    @user_validator
    def get(self, request):
        filename                        = urllib.parse.quote(
            f'[{timezone.localdate().strftime("%Y-%m-%d")}] 투자 내역 다운로드.xlsx'.encode('utf-8')
            )
        response                        = HttpResponse(content_type="application/vnd.ms-excel")
        response["Content-Disposition"] = 'attachment;filename*=UTF-8\'\'%s' % filename
        wb                              = xlwt.Workbook(encoding='ansi')
        ws                              = wb.add_sheet('투자내역')
        signed_user                     = request.user

        row_number = 0
        column_names = [
            '투자일',
            '상품호수', 
            '상품명', 
            '등급', 
            '예상수익률(%)', 
            '투자기간(개월)', 
            '투자금액', 
            '지급받은 원금', 
            '지급받은 이자',
            '세금', 
            '커미션'
            ]

        for index, column_name in enumerate(column_names):
            ws.write(row_number, index, column_name)


        investments = UserDeal.objects.filter(user=signed_user).select_related('deal').prefetch_related(
                    Prefetch('userpayback_set', to_attr='paybacks'),
                    Prefetch(
                        'userpayback_set', 
                        queryset=UserPayback.objects.filter(state=UserPayback.State.PAID.value), 
                        to_attr='paid_paybacks')
                    )

        rows = [
            [
                timezone.localtime(investment.created_at).strftime("%Y-%m-%d"),
                investment.id,
                investment.deal.name,
                Deal.Grade(investment.deal.grade).label,
                investment.deal.earning_rate,
                investment.deal.repayment_period,
                investment.amount,
                sum(paid_payback.principal for paid_payback in investment.paid_paybacks),
                sum(paid_payback.interest for paid_payback in investment.paid_paybacks),
                sum(paid_payback.tax for paid_payback in investment.paid_paybacks),
                sum(paid_payback.commission for paid_payback in investment.paid_paybacks)
            ] for investment in investments
        ]

        for row in rows:
            row_number +=1
            for column_number, attribute in enumerate(row):
                ws.write(row_number, column_number, attribute)

        wb.save(response)

        return response

class InvestmentDealView(View):
    @user_validator
    def post(self, request):
        try:
            user = request.user
            data = json.loads(request.body)

            user_deals = []
            for deal_data in data['investments']:
                deal             = Deal.objects.get(id=deal_data['id'], status=Deal.Status.APPLYING.value)
                amount           = deal_data['amount']
                payback_schedule = PaybackSchedule.objects.filter(deal=deal, option=amount)

                if not payback_schedule:
                    return JsonResponse({"message": "INVALID_OPTION"}, status=400) 

                user_deal = {
                    'deal'            : deal,
                    'amount'          : amount,
                    'payback_schedule': payback_schedule
                }
                user_deals.append(user_deal)

            with transaction.atomic():
                for user_deal in user_deals:

                    userdeal = UserDeal.objects.create(
                        deal   = user_deal['deal'],
                        user   = user,
                        amount = user_deal['amount']
                    )

                    UserPayback.objects.bulk_create([
                        UserPayback(
                            users_deals   = userdeal,
                            principal     = payback.principal,
                            interest      = payback.interest,
                            tax           = payback.tax,
                            commission    = payback.commission,
                            payback_round = payback.payback_round,
                            state         = UserPayback.State.TOBE_PAID.value,
                            payback_date  = payback.payback_date
                        ) for payback in user_deal['payback_schedule']
                    ])


            return JsonResponse({"message": "SUCCESS"}, status=201)

        except KeyError:
            return JsonResponse({"message": "KEY_ERROR"}, status=400)

        except Deal.DoesNotExist:
            return JsonResponse({"message": "INVALID_DEAL"}, status=400)
        
        except IntegrityError:
            return JsonResponse({"message": "INVESTD_DEAL"}, status=400)
            
    @user_validator
    def get(self, request):
        try:
            user     = request.user
            deals_id = request.GET.get('deals').split(",")
            deals    = Deal.objects.filter(id__in=deals_id)

            invest_info = [{
                    "id"              : deal.id,
                    "name"            : deal.name,
                    "category"        : Deal.Category(deal.category).label,
                    "grade"           : Deal.Grade(deal.grade).label,
                    "earningRate"     : deal.earning_rate,
                    "repaymentPeriod" : deal.repayment_period,
                    "amount"          : deal.userdeal_set.aggregate(total_price=Sum('amount'))['total_price'] or 0,
                    "investmentOption": [option.value for option in PaybackSchedule.Option]
            } for deal in deals]

            results = {
                'investInfo'     : invest_info,
                'depositAmount'  : user.deposit_amount,
                'name'           : user.name,
                'depositBank'    : user.deposit_bank.name,
                'depositAccount' : user.deposit_account,
            }

            return JsonResponse({"results" : results}, status=200)
        except Deal.DoesNotExist:                                   
            return JsonResponse({"message":"INVALID_ERROR"}, status=400)
