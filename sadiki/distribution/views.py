# -*- coding: utf-8 -*-
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render_to_response
from django.template import RequestContext, loader
from django.views.generic import TemplateView
from django.views.generic.base import View
from sadiki.core.models import Distribution, DISTRIBUTION_STATUS_END, Requestion, \
    STATUS_ON_DISTRIBUTION, STATUS_REQUESTER, Vacancies, Sadik, \
    DISTRIBUTION_STATUS_INITIAL, DISTRIBUTION_STATUS_ENDING, STATUS_DECISION, \
    SadikGroup, AgeGroup, STATUS_TEMP_DISTRIBUTED, STATUS_ON_TEMP_DISTRIBUTION, \
    STATUS_WANT_TO_CHANGE_SADIK, STATUS_ON_TRANSFER_DISTRIBUTION, Area
from sadiki.core.permissions import RequirePermissionsMixin
from sadiki.core.utils import get_current_distribution_year, run_command
from sadiki.core.workflow import DISTRIBUTION_INIT
from sadiki.distribution.forms import SelectSadikForm
from sadiki.logger.models import Logger
from sadiki.operator.views.base import OperatorPermissionMixin
import datetime


class DistributionInfo(RequirePermissionsMixin, TemplateView):
    u"""Главная страница + просмотр текущего расрпеделения"""
    required_permissions = ['is_operator', 'is_sadik_operator']
    template_name = 'distribution/distribution_info.html'

    def get(self, request, distribution_id=None):
        try:
            ending_distribution = Distribution.objects.get(status=DISTRIBUTION_STATUS_ENDING)
        except Distribution.DoesNotExist:
            pass
        else:
            return self.render_to_response({'ending_distribution': ending_distribution})
        if not distribution_id:
            return self.render_to_response(
                {
                    'finished_distributions': Distribution.objects.filter(
                        status=DISTRIBUTION_STATUS_END).order_by('-end_datetime'),
                    'distribution': Distribution.objects.active(),
                })
        distribution = get_object_or_404(Distribution, id=distribution_id)
        if distribution.status != DISTRIBUTION_STATUS_END:
            return HttpResponseForbidden(u'Распределение еще не было завершено')
        requestions_by_sadiks = []
        sadiks_ids = Requestion.objects.filter(distributed_in_vacancy__distribution=distribution
            ).distinct().values_list('distributed_in_vacancy__sadik_group__sadik', flat=True)
        for sadik in Sadik.objects.filter(id__in=sadiks_ids
            ).distinct().order_by('number'):
            requestions = Requestion.objects.filter(
                distributed_in_vacancy__distribution=distribution,
                distributed_in_vacancy__sadik_group__sadik=sadik).order_by(
                    '-birth_date').select_related('profile')
            if requestions:
                requestions_by_sadiks.append([sadik, requestions])
        return self.render_to_response({'current_distribution': distribution,
            'requestions_by_sadiks': requestions_by_sadiks})


class DistributionInit(OperatorPermissionMixin, TemplateView):
    u"""
    инициализируется распределение, при этом все путевки привязываются к
    распределению
    """
    required_permissions = ['is_distributor']
    template_name = 'distribution/distribution_start.html'

    def dispatch(self, request):
        if Distribution.objects.active():
            return HttpResponseForbidden(
                u'Есть незавершенное распределение')
        return super(DistributionInit, self).dispatch(request)

    def post(self, request):
        if request.POST.get('confirmation') == 'yes':
#            инициируем зачисление
            distribution = Distribution.objects.create(
                year=get_current_distribution_year())
            Vacancies.objects.filter(distribution__isnull=True,
                status__isnull=True).update(distribution=distribution)
            Logger.objects.create_for_action(DISTRIBUTION_INIT,
                 extra={'user': request.user, 'obj': distribution})
            Requestion.objects.filter(status=STATUS_REQUESTER).update(
                status=STATUS_ON_DISTRIBUTION)
            Requestion.objects.filter(status=STATUS_TEMP_DISTRIBUTED).update(
                status=STATUS_ON_TEMP_DISTRIBUTION)
            Requestion.objects.filter(status=STATUS_WANT_TO_CHANGE_SADIK).update(
                status=STATUS_ON_TRANSFER_DISTRIBUTION)
        return HttpResponseRedirect(reverse('decision_manager'))


class DecisionManager(OperatorPermissionMixin, View):
    u"""Обертка для распределения заявок"""
    required_permissions = ['is_distributor']
    
    def queue_info(self):
        u"""Собирается информация о очереди"""
        info_dict = {}
        distribution = Distribution.objects.filter(status=DISTRIBUTION_STATUS_INITIAL)
        full_queue = Requestion.objects.queue().confirmed().filter(Q(
            status__in=(STATUS_ON_DISTRIBUTION, STATUS_ON_TEMP_DISTRIBUTION,
                STATUS_ON_TRANSFER_DISTRIBUTION),
        ) | Q(
            status=STATUS_DECISION,
            distributed_in_vacancy__distribution=distribution
        )).select_related('benefit_category')
        free_places = SadikGroup.objects.active().aggregate(Sum('free_places'))['free_places__sum']
        queue = []
        distributed_requestions = Requestion.objects.decision_requestions().filter(
            distributed_in_vacancy__distribution=distribution)
        if distributed_requestions.exists():
            last_distributed_requestion = distributed_requestions.reverse()[0]
        else:
            last_distributed_requestion = None
        distributed_requestions_number = distributed_requestions.count()
        current_distribution_year = get_current_distribution_year()
        if free_places:
            #    ищем первую заявку, которая может быть распределена
            age_groups = AgeGroup.objects.filter(sadikgroup__free_places__gt=0
                ).distinct()
            requestions_with_places_query = Q()
        #    проходимся по всем группам в которых есть места и формируем запрос
            for age_group in age_groups:
        #        фильтруем по возрастной группе
                query_for_group = Q(birth_date__lte=age_group.max_birth_date(
                    current_distribution_year=current_distribution_year)) & \
                    Q(birth_date__gt=age_group.min_birth_date(
                        current_distribution_year=current_distribution_year))
        #        должны быть места в приоритетных ДОУ
                query_for_pref_sadiks = Q(pref_sadiks__groups__free_places__gt=0,
                                        pref_sadiks__groups__age_group=age_group,
                                        pref_sadiks__groups__active=True)
        #        либо указана возможность зачисления в любой ДОУ и в выбранной области есть ДОУ с местами
        #        или не указана область
                query_for_any_sadiks = Q(distribute_in_any_sadik=True) & (
                    Q(areas__isnull=True) | Q(areas__sadik__groups__free_places__gt=0,
                                            areas__sadik__groups__age_group=age_group,
                                            areas__sadik__groups__active=True))
        #        собираем все в один запрос
                requestions_with_places_query |= query_for_group & (query_for_pref_sadiks | query_for_any_sadiks)
        #    список заявок, которые могут быть зачислены
            requestions_with_places = full_queue.exclude(status=STATUS_DECISION
                ).exclude(admission_date__gt=datetime.date.today(),
                ).filter(requestions_with_places_query)
            if requestions_with_places.exists():
                current_requestion = requestions_with_places[0]
                info_dict.update({'current_requestion': current_requestion,
                    'current_requestion_age_groups': current_requestion.age_groups(
                        current_distribution_year=current_distribution_year)})
                current_requestion_index = full_queue.requestions_before(
                    current_requestion).count()
                if last_distributed_requestion:
                    last_distributed_index = full_queue.requestions_before(
                        last_distributed_requestion).count()
                    inactive_queue = full_queue[last_distributed_index:current_requestion_index]
                    queue = full_queue[current_requestion_index:current_requestion_index + 10]
                else:
                    inactive_queue = full_queue[:current_requestion_index]
                    queue = full_queue[current_requestion_index:current_requestion_index + 10]
            else:
                inactive_queue = []
            info_dict.update({'inactive_queue': inactive_queue})
        info_dict.update({'distribution': distribution, 'queue': queue,
            'free_places': free_places, 'decision_status': STATUS_DECISION,
            'last_distributed_requestion': last_distributed_requestion,
            'distributed_requestions_number': distributed_requestions_number})
        return info_dict
                
    def sadiks_for_requestion(self, requestion):
        u"""ДОУ в которые можно зачислить заявку"""
        # Все садики, где есть места для ребенка с учётом его возраста
        
        available_sadik_groups = SadikGroup.objects.appropriate_for_birth_date(
            requestion.birth_date).filter(active=True, free_places__gt=0)
#        должны быть включены только приоритетные ДОУ
        query_available_sadiks = Q(sadik__in=requestion.pref_sadiks.all())
#        и ДОУ из выбранных территориальных областей
        if requestion.distribute_in_any_sadik:
            if requestion.areas.exists():
                query_available_sadiks |= Q(sadik__area__in=requestion.areas.all())
            else:
                query_available_sadiks |= Q(sadik__area__in=Area.objects.all())
        available_sadiks_ids = available_sadik_groups.filter(
            query_available_sadiks).values_list('sadik', flat=True)
        available_sadiks = Sadik.objects.filter(id__in=available_sadiks_ids)
        pref_sadiks = requestion.pref_sadiks.filter(id__in=available_sadiks_ids)
        any_sadiks = Sadik.objects.exclude(
            id__in=pref_sadiks).filter(id__in=available_sadiks)
        return {'pref_sadiks': pref_sadiks, 'any_sadiks': any_sadiks}
    
    def decision_manager(self, request):
        from sadiki.core.workflow import DECISION, TRANSFER_APROOVED, PERMANENT_DECISION
        
        queue_info_dict = self.queue_info()
        
        if not queue_info_dict['queue']:
    #        если очереди нет, то оператор не может работать с заявками
            return render_to_response('distribution/decision_manager.html',
                queue_info_dict, context_instance=RequestContext(request),)
    
        # Сортировка "остальных" садиков, если у ребенка задан location
    #    if current_requestion.location:
    #        from sadiki.templatetags.sadiki_tags import distance_tag
    #        any_sadiks = sorted(any_sadiks, key=lambda x: distance_tag(current_requestion.location, x.location)['distance'])
        current_requestion = queue_info_dict['current_requestion']
        sadiks_info_dict = self.sadiks_for_requestion(current_requestion)
        any_sadiks = sadiks_info_dict['any_sadiks']
        pref_sadiks = sadiks_info_dict['pref_sadiks']
        if pref_sadiks:
            sadiks_query = pref_sadiks
            is_preferred_sadiks = True
        else:
            is_preferred_sadiks = False
            sadiks_query = any_sadiks
        sadiks_query.add_related_groups(only_active=True)
        if request.method == 'POST':
            form = SelectSadikForm(
                current_requestion, data=request.POST,
                is_preferred_sadiks=is_preferred_sadiks, sadiks_query=sadiks_query
            )
    
            if form.is_valid():
                sadik_id = form.cleaned_data.get('sadik', None)
                sadik = Sadik.objects.get(id=sadik_id)
                if current_requestion.status == STATUS_ON_DISTRIBUTION:
                    current_requestion.distribute_in_sadik_from_requester(sadik)
                    Logger.objects.create_for_action(DECISION, extra={'user': None, 'obj': current_requestion})
    
                if current_requestion.status == STATUS_ON_TEMP_DISTRIBUTION:
                    current_requestion.distribute_in_sadik_from_tempdistr(sadik)
                    Logger.objects.create_for_action(PERMANENT_DECISION, extra={'user': None, 'obj': current_requestion})
    
                if current_requestion.status == STATUS_ON_TRANSFER_DISTRIBUTION:
                    current_requestion.distribute_in_sadik_from_sadikchange(sadik)
                    Logger.objects.create_for_action(TRANSFER_APROOVED, extra={'user': None, 'obj': current_requestion})
                messages.info(request, u'''
                     Для заявки %s был назначен МДОУ %s
                     ''' % (current_requestion.requestion_number, sadik))
                return HttpResponseRedirect(reverse('decision_manager'))
        else:
            form = SelectSadikForm(current_requestion,
                is_preferred_sadiks=is_preferred_sadiks, sadiks_query=sadiks_query)
        queue_info_dict.update({'sadik_list': sadiks_query,
            'select_sadik_form': form,})
        return render_to_response('distribution/decision_manager.html',
            queue_info_dict, context_instance=RequestContext(request),
        )
    
    def get(self, *args, **kwargs):
        return self.decision_manager(*args, **kwargs)
    
    def post(self, *args, **kwargs):
        return self.decision_manager(*args, **kwargs)


class DistributionEnd(OperatorPermissionMixin, TemplateView):
    u"""старт процесса распределения мест"""
    required_permissions = ['is_distributor']
    template_name = 'distribution/distribution_end.html'
    
    def dispatch(self, request):
        try:
            start_distribution = Distribution.objects.get(
                status=DISTRIBUTION_STATUS_INITIAL)
        except Distribution.DoesNotExist:
            start_distribution = None
        if not all((self.check_permissions(request), start_distribution)):
            return HttpResponseForbidden(loader.render_to_string('403.html',
                context_instance=RequestContext(request)))

        return TemplateView.dispatch(self, request, start_distribution)

    def post(self, request, start_distribution):
        if request.POST.get('confirmation') == 'yes':
            start_distribution.status = DISTRIBUTION_STATUS_ENDING
            start_distribution.to_datetime = datetime.datetime.now()
            start_distribution.save()
            run_command('end_distribution', request.user.username)
        return HttpResponseRedirect(reverse('distribution_info', kwargs={'distribution_id': start_distribution.id}))
