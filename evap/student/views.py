from collections import OrderedDict

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import ugettext as _

from evap.evaluation.auth import participant_required
from evap.evaluation.models import Evaluation, NO_ANSWER, Semester

from evap.student.forms import QuestionnaireVotingForm
from evap.student.tools import question_id

from evap.results.tools import calculate_average_distribution, distribution_to_grade, textanswers_visible_to

SUCCESS_MAGIC_STRING = 'vote submitted successfully'


@participant_required
def index(request):
    # retrieve all evaluations, where the user is a participant and that are not new
    evaluations = list(set(Evaluation.objects.filter(participants=request.user).exclude(state="new")))
    for evaluation in evaluations:
        evaluation.distribution = calculate_average_distribution(evaluation)
        evaluation.avg_grade = distribution_to_grade(evaluation.distribution)

    voted_evaluations = list(set(Evaluation.objects.filter(voters=request.user)))
    due_evaluations = list(set(Evaluation.objects.filter(participants=request.user, state='in_evaluation').exclude(voters=request.user)))

    # due evaluations come first, then everything else in chronological order
    # some states are handled as a group because they appear the same to students
    sorter = lambda evaluation: (
        evaluation not in due_evaluations,
        evaluation.state not in ['prepared', 'editor_approved', 'approved'],
        evaluation.state != 'in_evaluation',
        evaluation.state not in ['evaluated', 'reviewed'],
        evaluation.name
    )
    evaluations.sort(key=sorter)

    semesters = Semester.objects.all()
    semester_list = [dict(
        semester_name=semester.name,
        id=semester.id,
        is_active_semester=semester.is_active_semester,
        results_are_archived=semester.results_are_archived,
        grade_documents_are_deleted=semester.grade_documents_are_deleted,
        evaluations=[evaluation for evaluation in evaluations if evaluation.semester_id == semester.id]
    ) for semester in semesters]

    template_data = dict(
        semester_list=semester_list,
        voted_evaluations=voted_evaluations,
        can_download_grades=request.user.can_download_grades,
    )
    return render(request, "student_index.html", template_data)


def get_valid_form_groups_or_render_vote_page(request, evaluation, preview, for_rendering_in_modal=False):
    contributions_to_vote_on = evaluation.contributions.all()
    # prevent a user from voting on themselves
    if not preview:
        contributions_to_vote_on = contributions_to_vote_on.exclude(contributor=request.user)

    form_groups = OrderedDict()
    for contribution in contributions_to_vote_on:
        questionnaires = contribution.questionnaires.all()
        if not questionnaires.exists():
            continue
        form_groups[contribution] = [QuestionnaireVotingForm(request.POST or None, contribution=contribution, questionnaire=questionnaire) for questionnaire in questionnaires]

    if all(all(form.is_valid() for form in form_group) for form_group in form_groups.values()):
        assert not preview
        return form_groups, None

    evaluation_form_group = form_groups.pop(evaluation.general_contribution)

    contributor_form_groups = [(contribution.contributor, contribution.label, form_group, any(form.errors for form in form_group), textanswers_visible_to(contribution)) for contribution, form_group in form_groups.items()]
    evaluation_form_group_top = [questions_form for questions_form in evaluation_form_group if questions_form.questionnaire.is_above_contributors]
    evaluation_form_group_bottom = [questions_form for questions_form in evaluation_form_group if questions_form.questionnaire.is_below_contributors]
    if not contributor_form_groups:
        evaluation_form_group_top += evaluation_form_group_bottom
        evaluation_form_group_bottom = []

    template_data = dict(
        errors_exist=any(any(form.errors for form in form_group) for form_group in form_groups.values()),
        evaluation_form_group_top=evaluation_form_group_top,
        evaluation_form_group_bottom=evaluation_form_group_bottom,
        contributor_form_groups=contributor_form_groups,
        evaluation=evaluation,
        small_evaluation_size_warning=evaluation.num_participants <= settings.SMALL_COURSE_SIZE,
        preview=preview,
        vote_end_datetime=evaluation.vote_end_datetime,
        hours_left_for_evaluation=evaluation.time_left_for_evaluation.seconds//3600,
        minutes_left_for_evaluation=(evaluation.time_left_for_evaluation.seconds//60)%60,
        success_magic_string=SUCCESS_MAGIC_STRING,
        success_redirect_url=reverse('student:index'),
        evaluation_ends_soon=evaluation.evaluation_ends_soon(),
        for_rendering_in_modal=for_rendering_in_modal,
        general_contribution_textanswers_visible_to=textanswers_visible_to(evaluation.general_contribution),
    )
    return None, render(request, "student_vote.html", template_data)


@participant_required
def vote(request, evaluation_id):
    evaluation = get_object_or_404(Evaluation, id=evaluation_id)
    if not evaluation.can_user_vote(request.user):
        raise PermissionDenied

    form_groups, rendered_page = get_valid_form_groups_or_render_vote_page(request, evaluation, preview=False)
    if rendered_page is not None:
        return rendered_page

    # all forms are valid, begin vote operation
    with transaction.atomic():
        # add user to evaluation.voters
        # not using evaluation.voters.add(request.user) since that fails silently when done twice.
        evaluation.voters.through.objects.create(userprofile_id=request.user.pk, evaluation_id=evaluation.pk)

        for contribution, form_group in form_groups.items():
            for questionnaire_form in form_group:
                questionnaire = questionnaire_form.questionnaire
                for question in questionnaire.questions.all():
                    if question.is_heading_question:
                        continue

                    identifier = question_id(contribution, questionnaire, question)
                    value = questionnaire_form.cleaned_data.get(identifier)

                    if question.is_text_question:
                        if value:
                            question.answer_class.objects.create(contribution=contribution, question=question, answer=value)
                    else:
                        if value != NO_ANSWER:
                            answer_counter, __ = question.answer_class.objects.get_or_create(contribution=contribution, question=question, answer=value)
                            answer_counter.count += 1
                            answer_counter.save()

        if not evaluation.can_publish_text_results:
            # enable text result publishing if first user confirmed that publishing is okay or second user voted
            if request.POST.get('text_results_publish_confirmation_top') == 'on' or request.POST.get('text_results_publish_confirmation_bottom') == 'on' or evaluation.voters.count() >= 2:
                evaluation.can_publish_text_results = True
                evaluation.save()

        evaluation.evaluation_evaluated.send(sender=Evaluation, request=request, semester=evaluation.semester)

    messages.success(request, _("Your vote was recorded."))
    return HttpResponse(SUCCESS_MAGIC_STRING)
