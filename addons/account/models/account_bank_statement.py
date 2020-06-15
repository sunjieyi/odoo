# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.osv import expression
from odoo.tools import float_is_zero
from odoo.tools import float_compare, float_round, float_repr
from odoo.tools.misc import formatLang, format_date
from odoo.exceptions import UserError, ValidationError

import time
import math
import base64
import re


class AccountCashboxLine(models.Model):
    """ Cash Box Details """
    _name = 'account.cashbox.line'
    _description = 'CashBox Line'
    _rec_name = 'coin_value'
    _order = 'coin_value'

    @api.depends('coin_value', 'number')
    def _sub_total(self):
        """ Calculates Sub total"""
        for cashbox_line in self:
            cashbox_line.subtotal = cashbox_line.coin_value * cashbox_line.number

    coin_value = fields.Float(string='Coin/Bill Value', required=True, digits=0)
    number = fields.Integer(string='#Coins/Bills', help='Opening Unit Numbers')
    subtotal = fields.Float(compute='_sub_total', string='Subtotal', digits=0, readonly=True)
    cashbox_id = fields.Many2one('account.bank.statement.cashbox', string="Cashbox")
    currency_id = fields.Many2one('res.currency', related='cashbox_id.currency_id')


class AccountBankStmtCashWizard(models.Model):
    """
    Account Bank Statement popup that allows entering cash details.
    """
    _name = 'account.bank.statement.cashbox'
    _description = 'Bank Statement Cashbox'

    cashbox_lines_ids = fields.One2many('account.cashbox.line', 'cashbox_id', string='Cashbox Lines')
    start_bank_stmt_ids = fields.One2many('account.bank.statement', 'cashbox_start_id')
    end_bank_stmt_ids = fields.One2many('account.bank.statement', 'cashbox_end_id')
    total = fields.Float(compute='_compute_total')
    currency_id = fields.Many2one('res.currency', compute='_compute_currency')

    @api.depends('start_bank_stmt_ids', 'end_bank_stmt_ids')
    def _compute_currency(self):
        for cashbox in self:
            cashbox.currency_id = False
            if cashbox.end_bank_stmt_ids:
                cashbox.currency_id = cashbox.end_bank_stmt_ids[0].currency_id
            if cashbox.start_bank_stmt_ids:
                cashbox.currency_id = cashbox.start_bank_stmt_ids[0].currency_id

    @api.depends('cashbox_lines_ids', 'cashbox_lines_ids.coin_value', 'cashbox_lines_ids.number')
    def _compute_total(self):
        for cashbox in self:
            cashbox.total = sum([line.subtotal for line in cashbox.cashbox_lines_ids])

    @api.model
    def default_get(self, fields):
        vals = super(AccountBankStmtCashWizard, self).default_get(fields)
        balance = self.env.context.get('balance')
        statement_id = self.env.context.get('statement_id')
        if 'start_bank_stmt_ids' in fields and not vals.get('start_bank_stmt_ids') and statement_id and balance == 'start':
            vals['start_bank_stmt_ids'] = [(6, 0, [statement_id])]
        if 'end_bank_stmt_ids' in fields and not vals.get('end_bank_stmt_ids') and statement_id and balance == 'close':
            vals['end_bank_stmt_ids'] = [(6, 0, [statement_id])]

        return vals

    def name_get(self):
        result = []
        for cashbox in self:
            result.append((cashbox.id, str(cashbox.total)))
        return result

    @api.model_create_multi
    def create(self, vals):
        cashboxes = super(AccountBankStmtCashWizard, self).create(vals)
        cashboxes._validate_cashbox()
        return cashboxes

    def write(self, vals):
        res = super(AccountBankStmtCashWizard, self).write(vals)
        self._validate_cashbox()
        return res

    def _validate_cashbox(self):
        for cashbox in self:
            if cashbox.start_bank_stmt_ids:
                cashbox.start_bank_stmt_ids.write({'balance_start': cashbox.total})
            if cashbox.end_bank_stmt_ids:
                cashbox.end_bank_stmt_ids.write({'balance_end_real': cashbox.total})


class AccountBankStmtCloseCheck(models.TransientModel):
    """
    Account Bank Statement wizard that check that closing balance is correct.
    """
    _name = 'account.bank.statement.closebalance'
    _description = 'Bank Statement Closing Balance'

    def validate(self):
        bnk_stmt_id = self.env.context.get('active_id', False)
        if bnk_stmt_id:
            self.env['account.bank.statement'].browse(bnk_stmt_id).button_validate()
        return {'type': 'ir.actions.act_window_close'}


class AccountBankStatement(models.Model):
    _name = "account.bank.statement"
    _description = "Bank Statement"
    _order = "date desc, name desc, id desc"
    _inherit = ['mail.thread', 'sequence.mixin']
    _check_company_auto = True

    # Note: the reason why we did 2 separate function with the same dependencies (one for balance_start and one for balance_end_real)
    # is because if we create a bank statement with a default value for one of the field but not the other, the compute method
    # won't be called and therefore the other field will have a value of 0 and we don't want that.
    @api.depends('previous_statement_id', 'previous_statement_id.balance_end_real')
    def _compute_starting_balance(self):
        for statement in self:
            if statement.previous_statement_id.balance_end_real != statement.balance_start:
                statement.balance_start = statement.previous_statement_id.balance_end_real
            else:
                # Need default value
                statement.balance_start = statement.balance_start or 0.0

    @api.depends('previous_statement_id', 'previous_statement_id.balance_end_real')
    def _compute_ending_balance(self):
        latest_statement = self.env['account.bank.statement'].search([('journal_id', '=', self[0].journal_id.id)], limit=1)
        for statement in self:
            # recompute balance_end_real in case we are in a bank journal and if we change the
            # balance_end_real of previous statement as we don't want
            # holes in case we add a statement in between 2 others statements.
            # We only do this for the bank journal as we use the balance_end_real in cash
            # journal for verification and creating cash difference entries so we don't want
            # to recompute the value in that case
            if statement.journal_type == 'bank':
                # If we are on last statement and that statement already has a balance_end_real, don't change the balance_end_real
                # Otherwise, recompute balance_end_real to prevent holes between statement.
                if latest_statement.id and statement.id == latest_statement.id and not float_is_zero(statement.balance_end_real, precision_digits=statement.currency_id.decimal_places):
                    statement.balance_end_real = statement.balance_end_real or 0.0
                else:
                    total_entry_encoding = sum([line.amount for line in statement.line_ids])
                    statement.balance_end_real = statement.previous_statement_id.balance_end_real + total_entry_encoding
            else:
                # Need default value
                statement.balance_end_real = statement.balance_end_real or 0.0

    @api.depends('line_ids', 'balance_start', 'line_ids.amount', 'balance_end_real')
    def _end_balance(self):
        for statement in self:
            statement.total_entry_encoding = sum([line.amount for line in statement.line_ids])
            statement.balance_end = statement.balance_start + statement.total_entry_encoding
            statement.difference = statement.balance_end_real - statement.balance_end

    def _is_difference_zero(self):
        for bank_stmt in self:
            bank_stmt.is_difference_zero = float_is_zero(bank_stmt.difference, precision_digits=bank_stmt.currency_id.decimal_places)

    @api.depends('journal_id')
    def _compute_currency(self):
        for statement in self:
            statement.currency_id = statement.journal_id.currency_id or statement.company_id.currency_id

    @api.depends('move_line_ids')
    def _get_move_line_count(self):
        for statement in self:
            statement.move_line_count = len(statement.move_line_ids)

    @api.model
    def _default_journal(self):
        journal_type = self.env.context.get('journal_type', False)
        company_id = self.env.company.id
        if journal_type:
            journals = self.env['account.journal'].search([('type', '=', journal_type), ('company_id', '=', company_id)])
            if journals:
                return journals[0]
        return self.env['account.journal']

    @api.depends('balance_start', 'previous_statement_id')
    def _compute_is_valid_balance_start(self):
        for bnk in self:
            bnk.is_valid_balance_start = float_is_zero(bnk.balance_start - bnk.previous_statement_id.balance_end_real, precision_digits=bnk.currency_id.decimal_places)

    @api.depends('date', 'journal_id')
    def _get_previous_statement(self):
        for st in self:
            # Search for the previous statement
            domain = [('date', '<=', st.date), ('journal_id', '=', st.journal_id.id)]
            # The reason why we have to perform this test is because we have two use case here:
            # First one is in case we are creating a new record, in that case that new record does
            # not have any id yet. However if we are updating an existing record, the domain date <= st.date
            # will find the record itself, so we have to add a condition in the search to ignore self.id
            if not isinstance(st.id, models.NewId):
                domain.append(('id', '!=', st.id))
            previous_statement = self.search(domain, limit=1)
            st.previous_statement_id = previous_statement.id

    name = fields.Char(string='Reference', states={'open': [('readonly', False)]}, copy=False, readonly=True)
    reference = fields.Char(string='External Reference', states={'open': [('readonly', False)]}, copy=False, readonly=True, help="Used to hold the reference of the external mean that created this statement (name of imported file, reference of online synchronization...)")
    date = fields.Date(required=True, states={'confirm': [('readonly', True)]}, index=True, copy=False, default=fields.Date.context_today)
    date_done = fields.Datetime(string="Closed On")
    balance_start = fields.Monetary(string='Starting Balance', states={'confirm': [('readonly', True)]}, compute='_compute_starting_balance', readonly=False, store=True)
    balance_end_real = fields.Monetary('Ending Balance', states={'confirm': [('readonly', True)]}, compute='_compute_ending_balance', readonly=False, store=True)
    state = fields.Selection(string='Status', required=True, readonly=True, copy=False, selection=[
            ('open', 'New'),
            ('posted', 'Processing'),
            ('confirm', 'Validated'),
        ], default='open',
        help="The current state of your bank statement:"
             "- New: Fully editable with draft Journal Entries."
             "- Processing: No longer editable with posted Journal entries, ready for the reconciliation."
             "- Validated: All lines are reconciled. There is nothing left to process.")
    currency_id = fields.Many2one('res.currency', compute='_compute_currency', string="Currency")
    journal_id = fields.Many2one('account.journal', string='Journal', required=True, states={'confirm': [('readonly', True)]}, default=_default_journal, check_company=True)
    journal_type = fields.Selection(related='journal_id.type', help="Technical field used for usability purposes")
    company_id = fields.Many2one('res.company', related='journal_id.company_id', string='Company', store=True, readonly=True,
        default=lambda self: self.env.company)

    total_entry_encoding = fields.Monetary('Transactions Subtotal', compute='_end_balance', store=True, help="Total of transaction lines.")
    balance_end = fields.Monetary('Computed Balance', compute='_end_balance', store=True, help='Balance as calculated based on Opening Balance and transaction lines')
    difference = fields.Monetary(compute='_end_balance', store=True, help="Difference between the computed ending balance and the specified ending balance.")

    line_ids = fields.One2many('account.bank.statement.line', 'statement_id', string='Statement lines', states={'confirm': [('readonly', True)]}, copy=True)
    move_line_ids = fields.One2many('account.move.line', 'statement_id', string='Entry lines', states={'confirm': [('readonly', True)]})
    move_line_count = fields.Integer(compute="_get_move_line_count")

    all_lines_reconciled = fields.Boolean(compute='_compute_all_lines_reconciled',
        help="Technical field indicating if all statement lines are fully reconciled.")
    user_id = fields.Many2one('res.users', string='Responsible', required=False, default=lambda self: self.env.user)
    cashbox_start_id = fields.Many2one('account.bank.statement.cashbox', string="Starting Cashbox")
    cashbox_end_id = fields.Many2one('account.bank.statement.cashbox', string="Ending Cashbox")
    is_difference_zero = fields.Boolean(compute='_is_difference_zero', string='Is zero', help="Check if difference is zero.")
    previous_statement_id = fields.Many2one('account.bank.statement', help='technical field to compute starting balance correctly', compute='_get_previous_statement', store=True)
    is_valid_balance_start = fields.Boolean(string="Is Valid Balance Start", store=True,
        compute="_compute_is_valid_balance_start",
        help="Technical field to display a warning message in case starting balance is different than previous ending balance")

    def write(self, values):
        res = super(AccountBankStatement, self).write(values)
        if values.get('date') or values.get('journal'):
            # If we are changing the date or journal of a bank statement, we have to change its previous_statement_id. This is done
            # automatically using the compute function, but we also have to change the previous_statement_id of records that were
            # previously pointing toward us and records that were pointing towards our new previous_statement_id. This is done here
            # by marking those record as needing to be recomputed.
            # Note that marking the field is not enough as we also have to recompute all its other fields that are depending on 'previous_statement_id'
            # hence the need to call modified afterwards.
            to_recompute = self.search([('previous_statement_id', 'in', self.ids), ('id', 'not in', self.ids)])
            if to_recompute:
                self.env.add_to_compute(self._fields['previous_statement_id'], to_recompute)
                to_recompute.modified(['previous_statement_id'])
            next_statements_to_recompute = self.search([('previous_statement_id', 'in', [st.previous_statement_id.id for st in self]), ('id', 'not in', self.ids)])
            if next_statements_to_recompute:
                self.env.add_to_compute(self._fields['previous_statement_id'], next_statements_to_recompute)
                next_statements_to_recompute.modified(['previous_statement_id'])
        return res

    @api.model_create_multi
    def create(self, values):
        res = super(AccountBankStatement, self).create(values)
        # Upon bank stmt creation, it is possible that the statement is inserted between two other statements and not at the end
        # In that case, we have to search for statement that are pointing to the same previous_statement_id as ourselve in order to
        # change their previous_statement_id to us. This is done by marking the field 'previous_statement_id' to be recomputed for such records.
        # Note that marking the field is not enough as we also have to recompute all its other fields that are depending on 'previous_statement_id'
        # hence the need to call modified afterwards.
        # The reason we are doing this here and not in a compute field is that it is not easy to write dependencies for such field.
        next_statements_to_recompute = self.search([('previous_statement_id', 'in', [st.previous_statement_id.id for st in res]), ('id', 'not in', res.ids)])
        if next_statements_to_recompute:
            self.env.add_to_compute(self._fields['previous_statement_id'], next_statements_to_recompute)
            next_statements_to_recompute.modified(['previous_statement_id'])
        return res

    @api.depends('line_ids.is_reconciled')
    def _compute_all_lines_reconciled(self):
        for statement in self:
            statement.all_lines_reconciled = all(st_line.is_reconciled for st_line in statement.line_ids)

    @api.onchange('journal_id')
    def onchange_journal_id(self):
        for st_line in self.line_ids:
            st_line.journal_id = self.journal_id
            st_line.currency_id = self.journal_id.currency_id or self.company_id.currency_id

    def _check_balance_end_real_same_as_computed(self):
        ''' Check the balance_end_real (encoded manually by the user) is equals to the balance_end (computed by odoo).
        In case of a cash statement, the different is set automatically to a profit/loss account.
        '''
        for stmt in self:
            if not stmt.currency_id.is_zero(stmt.difference):
                if stmt.journal_type == 'cash':
                    st_line_vals = {
                        'statement_id': stmt.id,
                        'journal_id': stmt.journal_id.id,
                        'amount': stmt.difference,
                        'date': stmt.date,
                    }

                    if stmt.difference < 0.0:
                        if not stmt.journal_id.loss_account_id:
                            raise UserError(_('Please go on the %s journal and define a Loss Account. This account will be used to record cash difference.', stmt.journal_id.name))

                        st_line_vals['payment_ref'] = _("Cash difference observed during the counting (Loss)")
                        self.env['account.bank.statement.line'].with_context(counterpart_account_id=stmt.journal_id.loss_account_id).create(st_line_vals)
                    else:
                        # statement.difference > 0.0
                        if not stmt.journal_id.profit_account_id:
                            raise UserError(_('Please go on the %s journal and define a Profit Account. This account will be used to record cash difference.', stmt.journal_id.name))

                        st_line_vals['payment_ref'] = _("Cash difference observed during the counting (Profit)")
                        self.env['account.bank.statement.line'].with_context(counterpart_account_id=stmt.journal_id.profit_account_id).create(st_line_vals)
                else:
                    balance_end_real = formatLang(self.env, stmt.balance_end_real, currency_obj=stmt.currency_id)
                    balance_end = formatLang(self.env, stmt.balance_end, currency_obj=stmt.currency_id)
                    raise UserError(_('The ending balance is incorrect !\nThe expected balance (%s) is different from the computed one. (%s)')
                        % (balance_end_real, balance_end))
        return True

    def unlink(self):
        for statement in self:
            if statement.state != 'open':
                raise UserError(_('In order to delete a bank statement, you must first cancel it to delete related journal items.'))
            # Explicitly unlink bank statement lines so it will check that the related journal entries have been deleted first
            statement.line_ids.unlink()
            # Some other bank statements might be link to this one, so in that case we have to switch the previous_statement_id
            # from that statement to the one linked to this statement
            next_statement = self.search([('previous_statement_id', '=', statement.id)])
            if next_statement:
                next_statement.previous_statement_id = statement.previous_statement_id
        return super(AccountBankStatement, self).unlink()

    # -------------------------------------------------------------------------
    # CONSTRAINT METHODS
    # -------------------------------------------------------------------------

    @api.constrains('journal_id')
    def _check_journal(self):
        for statement in self:
            if any(st_line.journal_id != statement.journal_id for st_line in statement.line_ids):
                raise ValidationError(_('The journal of a bank statement line must always be the same as the bank statement one.'))

    # -------------------------------------------------------------------------
    # BUSINESS METHODS
    # -------------------------------------------------------------------------

    def open_cashbox_id(self):
        self.ensure_one()
        context = dict(self.env.context or {})
        if context.get('balance'):
            context['statement_id'] = self.id
            if context['balance'] == 'start':
                cashbox_id = self.cashbox_start_id.id
            elif context['balance'] == 'close':
                cashbox_id = self.cashbox_end_id.id
            else:
                cashbox_id = False

            action = {
                'name': _('Cash Control'),
                'view_mode': 'form',
                'res_model': 'account.bank.statement.cashbox',
                'view_id': self.env.ref('account.view_account_bnk_stmt_cashbox_footer').id,
                'type': 'ir.actions.act_window',
                'res_id': cashbox_id,
                'context': context,
                'target': 'new'
            }

            return action

    def button_post(self):
        ''' Move the bank statements from 'draft' to 'posted'. '''
        if any(statement.state != 'open' for statement in self):
            raise UserError(_("Only new statements can be posted."))

        self._check_balance_end_real_same_as_computed()

        for statement in self:
            if not statement.name:
                statement._set_next_sequence()

        self.write({'state': 'posted'})
        self.line_ids.move_id.post()

    def button_validate(self):
        if any(statement.state != 'posted' or not statement.all_lines_reconciled for statement in self):
            raise UserError(_('All the account entries lines must be processed in order to validate the statement.'))

        for statement in self:

            # Chatter.
            statement.message_post(body=_('Statement %s confirmed.', statement.name))

            # Bank statement report.
            if statement.journal_id.type == 'bank':
                content, content_type = self.env.ref('account.action_report_account_statement')._render(statement.id)
                self.env['ir.attachment'].create({
                    'name': statement.name and _("Bank Statement %s.pdf", statement.name) or _("Bank Statement.pdf"),
                    'type': 'binary',
                    'datas': base64.encodebytes(content),
                    'res_model': statement._name,
                    'res_id': statement.id
                })

        self.write({'state': 'confirm', 'date_done': fields.Datetime.now()})

    def button_validate_or_action(self):
        if self.journal_type == 'cash' and not self.currency_id.is_zero(self.difference):
            action_rec = self.env['ir.model.data'].xmlid_to_object('account.action_view_account_bnk_stmt_check')
            if action_rec:
                action = action_rec.read()[0]
                return action

        return self.button_validate()

    def button_reopen(self):
        ''' Move the bank statements back to the 'open' state. '''
        if any(statement.state == 'draft' for statement in self):
            raise UserError(_("Only validated statements can be reset to new."))

        self.write({'state': 'open'})
        self.line_ids.move_id.button_draft()
        self.line_ids.button_undo_reconciliation()

    def button_journal_entries(self):
        return {
            'name': _('Journal Entries'),
            'view_mode': 'tree,form',
            'res_model': 'account.move',
            'view_id': False,
            'type': 'ir.actions.act_window',
            'domain': [('id', 'in', self.line_ids.move_id.ids)],
            'context': {
                'journal_id': self.journal_id.id,
            }
        }

    def _get_last_sequence_domain(self, relaxed=False):
        self.ensure_one()
        where_string = "WHERE journal_id = %(journal_id)s AND name != '/'"
        param = {'journal_id': self.journal_id.id}

        sequence_number_reset = self._deduce_sequence_number_reset(self.search([('date', '<', self.date)], order='date desc', limit=1).name)
        if not relaxed:
            if sequence_number_reset == 'year':
                where_string += " AND date_trunc('year', date) = date_trunc('year', %(date)s) "
                param['date'] = self.date
            elif sequence_number_reset == 'month':
                where_string += " AND date_trunc('month', date) = date_trunc('month', %(date)s) "
                param['date'] = self.date
        return where_string, param

    def _get_starting_sequence(self):
        self.ensure_one()
        last_sequence = self._get_last_sequence(relaxed=True)
        if last_sequence:
            sequence_number_reset = self._deduce_sequence_number_reset(self.search([('date', '<', self.date)], order='date desc', limit=1).name)
            if sequence_number_reset == 'year':
                sequence = re.match(self._sequence_yearly_regex, last_sequence)
                if sequence:
                    return '%s%04d%s%s%s' % (sequence.group('prefix1'), self.date.year, sequence.group('prefix2'), "0" * len(sequence.group('seq')), sequence.group('suffix'))
            elif sequence_number_reset == 'month':
                sequence = re.match(self._sequence_monthly_regex, last_sequence)
                if sequence:
                    return '%s%04d%s%02d%s%s%s' % (sequence.group('prefix1'), self.date.year, sequence.group('prefix2'), self.date.month, sequence.group('prefix3'), "0" * len(sequence.group('seq')), sequence.group('suffix'))

        # There was no pattern found, propose one
        return "%s %s %04d/%02d/00000" % (self.journal_id.code, _('Statement'), self.date.year, self.date.month)


class AccountBankStatementLine(models.Model):
    _name = "account.bank.statement.line"
    _inherits = {'account.move': 'move_id'}
    _description = "Bank Statement Line"
    _order = "statement_id desc, date, sequence, id desc"
    _check_company_auto = True

    # FIXME: Fields having the same name in both tables are confusing (partner_id & state). We don't change it because:
    # - It's a mess to track/fix.
    # - Some fields here could be simplified when the onchanges will be gone in account.move.
    # Should be improved in the future.

    # == Business fields ==
    move_id = fields.Many2one(
        comodel_name='account.move',
        string='Journal Entry', required=True, readonly=True, ondelete='cascade',
        check_company=True)
    statement_id = fields.Many2one(
        comodel_name='account.bank.statement',
        string='Statement', index=True, required=True, ondelete='cascade',
        check_company=True)

    sequence = fields.Integer(index=True, help="Gives the sequence order when displaying a list of bank statement lines.", default=1)
    account_number = fields.Char(string='Bank Account Number', help="Technical field used to store the bank account number before its creation, upon the line's processing")
    partner_name = fields.Char(
        help="This field is used to record the third party name when importing bank statement in electronic format, "
             "when the partner doesn't exist yet in the database (or cannot be found).")
    transaction_type = fields.Char(string='Transaction Type')
    payment_ref = fields.Char(string='Label', required=True)
    amount = fields.Monetary(currency_field='currency_id')
    amount_currency = fields.Monetary(currency_field='foreign_currency_id',
        help="The amount expressed in an optional other currency if it is a multi-currency entry.")
    foreign_currency_id = fields.Many2one('res.currency', string='Foreign Currency',
        help="The optional other currency if it is a multi-currency entry.")
    amount_residual = fields.Float(string="Residual Amount",
        compute="_compute_is_reconciled",
        store=True,
        help="The amount left to be reconciled on this statement line (signed according to its move lines' balance), expressed in its currency. This is a technical field use to speedup the application of reconciliation models.")
    currency_id = fields.Many2one('res.currency', string='Journal Currency')
    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='Partner', ondelete='restrict',
        domain="['|', ('parent_id','=', False), ('is_company','=',True)]",
        check_company=True)
    payment_ids = fields.Many2many(
        comodel_name='account.payment',
        relation='account_payment_account_bank_statement_line_rel',
        string='Auto-generated Payments',
        help="Payments generated during the reconciliation of this bank statement lines.")

    # == Display purpose fields ==
    is_reconciled = fields.Boolean(string='Is Reconciled', store=True,
        compute='_compute_is_reconciled',
        help="Technical field indicating if the statement line is already reconciled.")
    state = fields.Selection(related='statement_id.state', string='Status', readonly=True)

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    def _seek_for_lines(self):
        ''' Helper used to dispatch the journal items between:
        - The lines using the liquidity account.
        - The lines using the transfer account.
        - The lines being not in one of the two previous categories.
        :return: (liquidity_lines, suspense_lines, other_lines)
        '''
        liquidity_lines = self.env['account.move.line']
        suspense_lines = self.env['account.move.line']
        other_lines = self.env['account.move.line']

        for line in self.move_id.line_ids:
            if line.account_id in (self.journal_id.default_debit_account_id, self.journal_id.default_credit_account_id):
                liquidity_lines += line
            elif line.account_id == self.journal_id.suspense_account_id:
                suspense_lines += line
            else:
                other_lines += line
        return liquidity_lines, suspense_lines, other_lines

    @api.model
    def _prepare_liquidity_move_line_vals(self):
        ''' Prepare values to create a new account.move.line record corresponding to the
        liquidity line (having the bank/cash account).
        :return:        The values to create a new account.move.line record.
        '''
        self.ensure_one()

        statement = self.statement_id
        journal = statement.journal_id
        company_currency = journal.company_id.currency_id
        journal_currency = journal.currency_id if journal.currency_id != company_currency else False

        if self.foreign_currency_id and journal_currency:
            currency_id = journal_currency.id
            if self.foreign_currency_id == company_currency:
                amount_currency = self.amount
                balance = self.amount_currency
            else:
                amount_currency = self.amount
                balance = journal_currency._convert(amount_currency, journal.company_id.currency_id, journal.company_id, self.date)
        elif self.foreign_currency_id and not journal_currency:
            if self.foreign_currency_id == company_currency:
                amount_currency = 0.0
                balance = self.amount
                currency_id = False
            else:
                amount_currency = self.amount_currency
                balance = self.amount
                currency_id = self.foreign_currency_id.id
        elif not self.foreign_currency_id and journal_currency:
            currency_id = journal_currency.id
            amount_currency = self.amount
            balance = journal_currency._convert(amount_currency, journal.company_id.currency_id, journal.company_id, self.date)
        else:
            currency_id = False
            amount_currency = 0.0
            balance = self.amount

        return {
            'name': self.payment_ref,
            'move_id': self.move_id.id,
            'partner_id': self.partner_id.id,
            'currency_id': currency_id if amount_currency else False,
            'account_id': journal.default_debit_account_id.id if balance >= 0 else journal.default_credit_account_id.id,
            'debit': balance > 0 and balance or 0.0,
            'credit': balance < 0 and -balance or 0.0,
            'amount_currency': amount_currency if currency_id else False,
        }

    @api.model
    def _prepare_counterpart_move_line_vals(self, counterpart_vals, move_line=None):
        ''' Prepare values to create a new account.move.line move_line.
        By default, without specified 'counterpart_vals' or 'move_line', the counterpart line is
        created using the suspense account. Otherwise, this method is also called during the
        reconciliation to prepare the statement line's journal entry. In that case,
        'counterpart_vals' will be used to create a custom account.move.line (from the reconciliation widget)
        and 'move_line' will be used to create the counterpart of an existing account.move.line to which
        the newly created journal item will be reconciled.
        :param counterpart_vals:    A python dictionary containing:
            'balance':                  Optional amount to consider during the reconciliation. If a foreign currency is set on the
                                        counterpart line in the same foreign currency as the statement line, then this amount is
                                        considered as the amount in foreign currency. If not specified, the full balance is took.
                                        This value must be provided if move_line is not.
            'amount_residual':          The residual amount to reconcile expressed in the company's currency.
                                        /!\ This value should be equivalent to move_line.amount_residual except we want
                                        to avoid browsing the record when the only thing we need in an overview of the
                                        reconciliation, for example in the reconciliation widget.
            'amount_residual_currency': The residual amount to reconcile expressed in the foreign's currency.
                                        Using this key doesn't make sense without passing 'currency_id' in vals.
                                        /!\ This value should be equivalent to move_line.amount_residual_currency except
                                        we want to avoid browsing the record when the only thing we need in an overview
                                        of the reconciliation, for example in the reconciliation widget.
            **kwargs:                   Additional values that need to land on the account.move.line to create.
        :param move_line:           An optional account.move.line move_line representing the counterpart line to reconcile.
        :return:                    The values to create a new account.move.line move_line.
        '''
        self.ensure_one()

        statement = self.statement_id
        journal = statement.journal_id
        company_currency = journal.company_id.currency_id
        journal_currency = journal.currency_id if journal.currency_id != company_currency else False
        statement_line_rate = self.amount_currency / (self.amount or 1.0)

        balance_to_reconcile = counterpart_vals.pop('balance', None)
        amount_residual = -counterpart_vals.pop('amount_residual', move_line.amount_residual if move_line else 0.0) \
            if balance_to_reconcile is None else balance_to_reconcile
        amount_residual_currency = -counterpart_vals.pop('amount_residual_currency', move_line.amount_residual_currency if move_line else 0.0)\
            if balance_to_reconcile is None else balance_to_reconcile

        if 'currency_id' in counterpart_vals:
            currency_id = counterpart_vals['currency_id']
        elif move_line:
            currency_id = move_line.currency_id.id
        else:
            currency_id = self.foreign_currency_id.id

        if currency_id:
            if self.foreign_currency_id and journal_currency:

                # Different currencies set on the company, the journal, the statement line and the counterpart line.
                # Since we can express only one foreign currency on a journal item, only the foreign currency
                # will be used on the journal item we are created. However, in any case, the rate used by the
                # statement line is kept to convert from the journal's currency to the foreign's currency.

                if currency_id == self.foreign_currency_id.id:

                    # The counterpart currency if the same as the foreign currency set on the statement line.
                    # There is also a foreign currency set on the journal so the journal item to create will
                    # use the foreign currency set on the statement line.

                    amount_currency = amount_residual_currency
                    balance = journal_currency._convert(amount_currency / statement_line_rate, company_currency, journal.company_id, self.date)

                elif currency_id == journal_currency.id and self.foreign_currency_id == company_currency:

                    # The counterpart currency if the same as the foreign currency set on the journal.
                    # There is also a foreign currency set on the statement line that is the same as the company one.
                    # Then, the journal item to create will use the company's currency.

                    amount_currency = amount_residual_currency
                    balance = amount_currency * statement_line_rate
                    currency_id = False
                    amount_currency = 0.0

                elif currency_id == journal_currency.id and self.foreign_currency_id != company_currency:

                    # The counterpart currency if the same as the foreign currency set on the journal.
                    # There is also a foreign currency set on the statement line.
                    # The residual amount will be convert to the foreign currency set on the statement line.

                    amount_currency = amount_residual_currency
                    balance = journal_currency._convert(amount_currency, company_currency, journal.company_id, self.date)
                    amount_currency *= statement_line_rate
                    currency_id = self.foreign_currency_id.id

                else:

                    # Whatever the currency set on the journal item passed as parameter, the counterpart line
                    # will be expressed in the foreign currency set on the statement line.

                    balance = amount_residual
                    amount_currency = company_currency._convert(balance, journal_currency, journal.company_id, self.date)
                    amount_currency *= statement_line_rate
                    currency_id = self.foreign_currency_id.id

            elif self.foreign_currency_id and not journal_currency:

                # Different currencies set on the company, the statement line and the counterpart line.
                # In that case, the 'amount' set on the statement line is expressed in the company's currency
                # and is used as conversion rate between the company's currency and the foreign currency.

                if currency_id == self.foreign_currency_id.id:
                    amount_currency = amount_residual_currency
                    balance = amount_currency / statement_line_rate
                else:
                    balance = amount_residual
                    amount_currency = balance * statement_line_rate
                    currency_id = self.foreign_currency_id.id

            elif not self.foreign_currency_id and journal_currency:

                # Different currencies set on the company, the journal and the counterpart line.
                # Everything will be expressed in the journal's currency.

                if currency_id == journal_currency.id:
                    amount_currency = amount_residual_currency
                    balance = journal_currency._convert(amount_currency, company_currency, journal.company_id, self.date)
                else:
                    balance = amount_residual
                    amount_currency = company_currency._convert(balance, journal_currency, journal.company_id, self.date)
                    currency_id = journal_currency.id

            else:

                # Only a foreign currency set on the counterpart line.
                # Ignore it and record the line using the company's currency.

                balance = amount_residual
                amount_currency = 0.0
                currency_id = False

        else:
            balance = amount_residual

            if self.foreign_currency_id and journal_currency:

                # Different currencies set on the company, the journal and the statement line.
                # Obviously, the foreign currency set on the statement line will be set on the journal item
                # to create. Again, keep the same conversion rate as the statement line when converting from
                # the journal's currency to its foreign currency.

                if self.foreign_currency_id == company_currency:
                    amount_currency = 0.0
                else:
                    amount_currency = company_currency._convert(balance, journal_currency, journal.company_id, self.date)
                    amount_currency *= statement_line_rate
                    currency_id = self.foreign_currency_id.id

            elif self.foreign_currency_id and not journal_currency:

                # Different currencies set on the company and the statement line.
                # Record the counterpart line using the foreign currency.

                amount_currency = balance * statement_line_rate
                currency_id = self.foreign_currency_id.id

            elif not self.foreign_currency_id and journal_currency:

                # Different currencies set on the company and the journal.
                # Record the counterpart line using the company's currency.

                amount_currency = company_currency._convert(balance, journal_currency, journal.company_id, self.date)
                currency_id = journal_currency.id

            else:

                # Single currency.

                amount_currency = 0.0

        return {
            **counterpart_vals,
            'name': counterpart_vals.get('name', move_line.name if move_line else ''),
            'move_id': self.move_id.id,
            'partner_id': self.partner_id.id,
            'currency_id': currency_id if amount_currency else False,
            'account_id': counterpart_vals.get('account_id', move_line.account_id.id if move_line else False),
            'debit': balance if balance > 0 else 0.0,
            'credit': -balance if balance < 0 else 0.0,
            'amount_currency': amount_currency,
        }

    @api.model
    def _prepare_move_line_default_vals(self, counterpart_account_id=None):
        ''' Prepare the dictionary to create the default account.move.lines for the current account.bank.statement.line
        record.
        :return: A list of python dictionary to be passed to the account.move.line's 'create' method.
        '''
        self.ensure_one()

        if not counterpart_account_id:
            counterpart_account_id = self.journal_id.suspense_account_id.id

        if not counterpart_account_id:
            raise UserError(_(
                "You can't create a new statement line without a suspense account set on the %s journal."
            ) % self.journal_id.display_name)

        liquidity_line_vals = self._prepare_liquidity_move_line_vals()

        counterpart_vals = {
            'name': self.payment_ref,
            'account_id': counterpart_account_id,
        }

        if liquidity_line_vals['currency_id']:
            # Ensure the counterpart will have a balance exactly equals to the amount in journal currency.
            # This avoid some rounding issues when the currency rate between two currencies is not symmetrical.
            # E.g:
            # A.convert(amount_a, B) = amount_b
            # B.convert(amount_b, A) = amount_c != amount_a

            counterpart_vals.update({
                'currency_id': liquidity_line_vals['currency_id'],
                'balance': -liquidity_line_vals['amount_currency'],
            })
        else:
            counterpart_vals['balance'] = liquidity_line_vals['credit'] - liquidity_line_vals['debit']

        counterpart_line_vals = self._prepare_counterpart_move_line_vals(counterpart_vals)
        return [liquidity_line_vals, counterpart_line_vals]

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------

    @api.depends('currency_id', 'amount', 'foreign_currency_id', 'amount_currency',
                 'move_id.line_ids', 'move_id.line_ids.matched_debit_ids', 'move_id.line_ids.matched_credit_ids')
    def _compute_is_reconciled(self):
        ''' Compute the field indicating if the statement lines are already reconciled with something.
        This field is used for display purpose (e.g. display the 'cancel' button on the statement lines).
        Also computes the residual amount of the statement line.
        '''
        for st_line in self:
            liquidity_lines, suspense_lines, other_lines = st_line._seek_for_lines()

            # Compute is_reconciled
            if not st_line.id or suspense_lines:
                # New record: The journal items are not yet there.
                st_line.is_reconciled = False
            elif st_line.currency_id.is_zero(st_line.amount):
                st_line.is_reconciled = True
            else:
                # The journal entry seems reconciled.
                st_line.is_reconciled = True

            # Compute residual amount
            st_line_currency = suspense_lines.currency_id or suspense_lines.company_currency_id
            balance_field, residual_field = ('amount_currency', 'amount_residual_currency') if suspense_lines.currency_id else ('balance', 'amount_residual')

            if st_line.to_check:
                st_line.amount_residual = -st_line.amount_currency if st_line.foreign_currency_id else -st_line.amount
            elif suspense_lines.account_id.reconcile:
                st_line.amount_residual = sum(suspense_lines.mapped(residual_field))
            else:
                st_line.amount_residual = sum(suspense_lines.mapped(balance_field))

    # -------------------------------------------------------------------------
    # CONSTRAINT METHODS
    # -------------------------------------------------------------------------

    @api.constrains('amount', 'amount_currency', 'currency_id', 'foreign_currency_id', 'journal_id')
    def _check_amounts_currencies(self):
        ''' Ensure the consistency the specified amounts and the currencies. '''
        for st_line in self:
            if st_line.journal_id != st_line.statement_id.journal_id:
                raise ValidationError(_('The journal of a statement line must always be the same as the bank statement one.'))
            if st_line.currency_id.is_zero(st_line.amount):
                raise ValidationError(_("The amount of a statement line can't be equal to zero."))
            if st_line.foreign_currency_id == st_line.currency_id:
                raise ValidationError(_("The foreign currency must be different than the journal one: %s", st_line.currency_id.name))
            if st_line.foreign_currency_id and st_line.foreign_currency_id.is_zero(st_line.amount_currency):
                raise ValidationError(_("The amount in foreign currency must be set if the amount is not equal to zero."))
            if not st_line.foreign_currency_id and st_line.amount_currency:
                raise ValidationError(_("You can't provide an amount in foreign currency without specifying a foreign currency."))

    # -------------------------------------------------------------------------
    # LOW-LEVEL METHODS
    # -------------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        # OVERRIDE
        counterpart_account_ids = []

        for vals in vals_list:
            statement = self.env['account.bank.statement'].browse(vals['statement_id'])
            if statement.state != 'open' and self._context.get('check_move_validity', True):
                raise UserError(_("You can only create statement line in open bank statements."))

            # Force the move_type to avoid inconsistency with residual 'default_move_type' inside the context.
            vals['move_type'] = 'entry'

            journal = statement.journal_id
            # Ensure the journal is the same as the statement one.
            vals['journal_id'] = journal.id
            vals['currency_id'] = (journal.currency_id or journal.company_id.currency_id).id
            if 'date' not in vals:
                vals['date'] = statement.date

            # Hack to force different account instead of the suspense account.
            counterpart_account_ids.append(vals.pop('counterpart_account_id', None))

        st_lines = super().create(vals_list)

        for i, st_line in enumerate(st_lines):
            counterpart_account_id = counterpart_account_ids[i]

            to_write = {'statement_line_id': st_line.id}
            if 'line_ids' not in vals_list[i]:
                to_write['line_ids'] = [(0, 0, line_vals) for line_vals in st_line._prepare_move_line_default_vals(counterpart_account_id=counterpart_account_id)]

            st_line.move_id.write(to_write)

        return st_lines

    def write(self, vals):
        # OVERRIDE
        res = super().write(vals)
        self._synchronize_to_moves(set(vals.keys()))
        return res

    def unlink(self):
        # OVERRIDE to unlink the inherited account.move (move_id field) as well.
        moves = self.with_context(force_delete=True).mapped('move_id')
        res = super().unlink()
        moves.unlink()
        return res

    # -------------------------------------------------------------------------
    # SYNCHRONIZATION account.bank.statement.line <-> account.move
    # -------------------------------------------------------------------------

    def _synchronize_from_moves(self, changed_fields):
        ''' Update the account.bank.statement.line regarding its related account.move.
        Also, check both models are still consistent.
        :param changed_fields: A set containing all modified fields on account.move.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        for st_line in self.with_context(skip_account_move_synchronization=True):
            move = st_line.move_id
            move_vals_to_write = {}
            st_line_vals_to_write = {}

            if 'state' in changed_fields:
                if (st_line.state == 'open' and move.state != 'draft') or (st_line.state == 'posted' and move.state != 'posted'):
                    raise UserError(_(
                        "You can't manually change the state of journal entry %s, as it has been created by bank "
                        "statement %s."
                    ) % (st_line.move_id.display_name, st_line.statement_id.display_name))

            if 'line_ids' in changed_fields:
                liquidity_lines, suspense_lines, other_lines = st_line._seek_for_lines()
                company_currency = st_line.journal_id.company_id.currency_id
                journal_currency = st_line.journal_id.currency_id if st_line.journal_id.currency_id != company_currency else False

                if len(liquidity_lines) != 1:
                    raise UserError(_(
                        "The journal entry %s reached an invalid state regarding its related statement line.\n"
                        "To be consistent, the journal entry must always have exactly one journal item involving the "
                        "bank/cash account."
                    ) % st_line.move_id.display_name)

                st_line_vals_to_write.update({
                    'payment_ref': liquidity_lines.name,
                    'partner_id': liquidity_lines.partner_id.id,
                })

                # Update 'amount' according to the liquidity line.

                if journal_currency:
                    st_line_vals_to_write.update({
                        'amount': liquidity_lines.amount_currency,
                    })
                else:
                    st_line_vals_to_write.update({
                        'amount': liquidity_lines.balance,
                    })

                if len(suspense_lines) == 1:

                    if journal_currency and suspense_lines.currency_id == journal_currency:

                        # The suspense line is expressed in the journal's currency meaning the foreign currency
                        # set on the statement line is no longer needed.

                        st_line_vals_to_write.update({
                            'amount_currency': 0.0,
                            'foreign_currency_id': False,
                        })

                    elif not suspense_lines.currency_id and st_line.foreign_currency_id == company_currency:

                        # The suspense line has no foreign currency because the foreign currency set on the
                        # statement line is the same as the company one. In that case, don't erase the
                        # 'foreign_currency_id' field.

                        st_line_vals_to_write.update({
                            'amount_currency': -suspense_lines.balance,
                        })

                    else:

                        # Update the statement line regarding the foreign currency of the suspense line.

                        st_line_vals_to_write.update({
                            'amount_currency': -suspense_lines.amount_currency,
                            'foreign_currency_id': suspense_lines.currency_id.id,
                        })

                move_vals_to_write.update({
                    'partner_id': liquidity_lines.partner_id.id,
                    'currency_id': (st_line.foreign_currency_id or journal_currency or company_currency).id,
                })

            move.write(move._cleanup_write_orm_values(move, move_vals_to_write))
            st_line.write(move._cleanup_write_orm_values(st_line, st_line_vals_to_write))

    def _synchronize_to_moves(self, changed_fields):
        ''' Update the account.move regarding the modified account.bank.statement.line.
        :param changed_fields: A list containing all modified fields on account.bank.statement.line.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        if not any(field_name in changed_fields for field_name in (
            'payment_ref', 'amount', 'amount_currency',
            'foreign_currency_id', 'currency_id', 'partner_id',
        )):
            return

        for st_line in self.with_context(skip_account_move_synchronization=True):
            liquidity_lines, suspense_lines, other_lines = st_line._seek_for_lines()
            company_currency = st_line.journal_id.company_id.currency_id
            journal_currency = st_line.journal_id.currency_id if st_line.journal_id.currency_id != company_currency else False

            line_vals_list = self._prepare_move_line_default_vals()
            line_ids_commands = [(1, liquidity_lines.id, line_vals_list[0])]

            if suspense_lines:
                line_ids_commands.append((1, suspense_lines.id, line_vals_list[1]))
            else:
                line_ids_commands.append((0, 0, line_vals_list[1]))

            for line in other_lines:
                line_ids_commands.append((2, line.id))

            st_line.move_id.write({
                'partner_id': st_line.partner_id.id,
                'currency_id': (st_line.foreign_currency_id or journal_currency or company_currency).id,
                'line_ids': line_ids_commands,
            })

    # -------------------------------------------------------------------------
    # RECONCILIATION METHODS
    # -------------------------------------------------------------------------

    def _prepare_reconciliation(self, lines_vals_list, create_payment_for_invoice=False):
        ''' Helper for the "reconcile" method used to get a full preview of the reconciliation result. This method is
        quite useful to deal with reconcile models or the reconciliation widget because it ensures the values seen by
        the user are exactly the values you get after reconciling.

        :param lines_vals_list:             See the 'reconcile' method.
        :param create_payment_for_invoice:  A flag indicating the statement line must create payments on the fly during
                                            the reconciliation.
        :return: The diff to be applied on the statement line as a tuple
        (
            lines_to_create:    The values to create the account.move.line on the statement line.
            payments_to_create: The values to create the account.payments.
            open_balance_vals:  A dictionary to create the open-balance line or None if the reconciliation is full.
            existing_lines:     The counterpart lines to which the reconciliation will be done.
        )
        '''

        self.ensure_one()

        liquidity_lines, suspense_lines, other_lines = self._seek_for_lines()

        # Ensure the statement line has not yet been already reconciled.
        # If the move has 'to_check' enabled, it means the statement line has created some lines that
        # need to be checked later and replaced by the real ones.
        if not self.move_id.to_check and other_lines:
            raise UserError(_("The statement line has already been reconciled."))

        # A list of dictionary containing:
        # - line_vals:          The values to create the account.move.line on the statement line.
        # - payment_vals:       The optional values to create a bridge account.payment
        # - counterpart_line:   The optional counterpart line to reconcile with 'line'.
        reconciliation_overview = []

        total_balance = liquidity_lines.balance

        # Step 1: Split 'lines_vals_list' into two batches:
        # - The existing account.move.lines that need to be reconciled with the statement line.
        #       => Will be managed at step 2.
        # - The account.move.lines to be created from scratch.
        #       => Will be managed directly.

        to_browse_ids = []
        to_process_vals = []
        for vals in lines_vals_list:
            # Don't modify the params directly.
            vals = dict(vals)

            if 'id' in vals:
                # Existing account.move.line.
                to_browse_ids.append(vals.pop('id'))
                to_process_vals.append(vals)
            else:
                # Newly created account.move.line from scratch.
                line_vals = self._prepare_counterpart_move_line_vals(vals)
                total_balance += line_vals['debit'] - line_vals['credit']

                reconciliation_overview.append({
                    'line_vals': line_vals,
                })

        # Step 2: Browse counterpart lines all in one and process them.

        existing_lines = self.env['account.move.line'].browse(to_browse_ids)
        for line, counterpart_vals in zip(existing_lines, to_process_vals):
            line_vals = self._prepare_counterpart_move_line_vals(counterpart_vals, move_line=line)
            balance = line_vals['debit'] - line_vals['credit']

            reconciliation_vals = {
                'line_vals': line_vals,
                'counterpart_line': line,
            }

            if create_payment_for_invoice and line.account_internal_type in ('receivable', 'payable'):

                # Prepare values to create a new account.payment.
                payment_vals = self.env['account.payment.register']\
                    .with_context(active_model='account.move.line', active_ids=line.ids)\
                    .create({
                        'amount': abs(line_vals['amount_currency']) if line_vals['currency_id'] else abs(balance),
                        'payment_date': self.date,
                        'payment_type': 'inbound' if balance < 0.0 else 'outbound',
                        'journal_id': self.journal_id.id,
                        'currency_id': (self.foreign_currency_id or self.currency_id).id,
                     })\
                     ._create_payment_vals_from_wizard()

                if payment_vals['payment_type'] == 'inbound':
                    liquidity_account = self.journal_id.payment_debit_account_id
                else:
                    liquidity_account = self.journal_id.payment_credit_account_id

                # Preserve the rate of the statement line.
                payment_vals['line_ids'] = [
                    # Receivable / Payable line.
                    (0, 0, {
                        **line_vals,
                    }),

                    # Liquidity line.
                    (0, 0, {
                        **line_vals,
                        'amount_currency': -line_vals['amount_currency'],
                        'debit': line_vals['credit'],
                        'credit': line_vals['debit'],
                        'account_id': liquidity_account.id,
                    }),
                ]

                # Prepare the line to be reconciled with the payment.
                if payment_vals['payment_type'] == 'inbound':
                    # Receive money.
                    line_vals['account_id'] = self.journal_id.payment_debit_account_id.id
                elif payment_vals['payment_type'] == 'outbound':
                    # Send money.
                    line_vals['account_id'] = self.journal_id.payment_credit_account_id.id

                reconciliation_vals['payment_vals'] = payment_vals

            reconciliation_overview.append(reconciliation_vals)

            total_balance += balance

        # Step 3: If the journal entry is not yet balanced, create an open balance.

        if self.company_currency_id.round(total_balance):
            if self.amount > 0:
                open_balance_account = self.partner_id.with_company(self.company_id).property_account_receivable_id
            else:
                open_balance_account = self.partner_id.with_company(self.company_id).property_account_payable_id

            open_balance_vals = self._prepare_counterpart_move_line_vals({
                'name': '%s: %s' % (self.payment_ref, _('Open Balance')),
                'account_id': open_balance_account.id,
                'balance': -total_balance,
                'currency_id': False,
            })
        else:
            open_balance_vals = None

        return reconciliation_overview, open_balance_vals

    def reconcile(self, lines_vals_list, to_check=False):
        ''' Perform a reconciliation on the current account.bank.statement.line with some
        counterpart account.move.line.
        If the statement line entry is not fully balanced after the reconciliation, an open balance will be created
        using the partner.

        :param lines_vals_list: A list of python dictionary containing:
            'id':               Optional id of an existing account.move.line.
                                For each line having an 'id', a new line will be created in the current statement line.
            'balance':          Optional amount to consider during the reconciliation. If a foreign currency is set on the
                                counterpart line in the same foreign currency as the statement line, then this amount is
                                considered as the amount in foreign currency. If not specified, the full balance is taken.
                                This value must be provided if 'id' is not.
            **kwargs:           Custom values to be set on the newly created account.move.line.
        :param to_check:        Mark the current statement line as "to_check" (see field for more details).
        '''
        self.ensure_one()
        liquidity_lines, suspense_lines, other_lines = self._seek_for_lines()

        reconciliation_overview, open_balance_vals = self._prepare_reconciliation(lines_vals_list)

        # ==== Manage res.partner.bank ====

        if self.account_number and self.partner_id and not self.partner_bank_id:
            self.partner_bank_id = self._find_or_create_bank_account()

        # ==== Check open balance ====

        if open_balance_vals:
            if not open_balance_vals.get('partner_id'):
                raise UserError(_("Unable to create an open balance for a statement line without a partner set."))
            if not open_balance_vals.get('account_id'):
                raise UserError(_("Unable to create an open balance for a statement line because the receivable "
                                  "/ payable accounts are missing on the partner."))

        # ==== Create & reconcile payments ====
        # When reconciling to a receivable/payable account, create an payment on the fly.

        pay_reconciliation_overview = [reconciliation_vals
                                       for reconciliation_vals in reconciliation_overview
                                       if reconciliation_vals.get('payment_vals')]
        if pay_reconciliation_overview:
            payment_vals_list = [reconciliation_vals['payment_vals'] for reconciliation_vals in pay_reconciliation_overview]
            payments = self.env['account.payment'].create(payment_vals_list)

            payments.action_post()

            for reconciliation_vals, payment in zip(pay_reconciliation_overview, payments):
                reconciliation_vals['payment'] = payment

                # Reconcile the newly created payment with the counterpart line.
                (reconciliation_vals['counterpart_line'] + payment.line_ids)\
                    .filtered(lambda line: line.account_id == reconciliation_vals['counterpart_line'].account_id)\
                    .reconcile()

        # ==== Create & reconcile lines on the bank statement line ====

        to_create_commands = [(0, 0, open_balance_vals)] if open_balance_vals else []
        to_delete_commands = [(2, line.id) for line in suspense_lines + other_lines]

        # Cleanup previous lines.
        self.move_id.with_context(check_move_validity=False, skip_account_move_synchronization=True).write({
            'line_ids': to_delete_commands + to_create_commands,
            'to_check': to_check,
        })

        line_vals_list = [reconciliation_vals['line_vals'] for reconciliation_vals in reconciliation_overview]
        new_lines = self.env['account.move.line'].create(line_vals_list)
        for reconciliation_vals, line in zip(reconciliation_overview, new_lines):
            if reconciliation_vals.get('payment'):
                accounts = (self.journal_id.payment_debit_account_id, self.journal_id.payment_credit_account_id)
                counterpart_line = reconciliation_vals['payment'].line_ids.filtered(lambda line: line.account_id in accounts)
            elif reconciliation_vals.get('counterpart_line'):
                counterpart_line = reconciliation_vals['counterpart_line']
            else:
                continue

            (line + counterpart_line).reconcile()

            # Update the payment date to match the current bank statement line's date.
            if counterpart_line.payment_id:
                counterpart_line.payment_id.date = self.date

    # -------------------------------------------------------------------------
    # BUSINESS METHODS
    # -------------------------------------------------------------------------

    def _find_or_create_bank_account(self):
        bank_account = self.env['res.partner.bank'].search(
            [('company_id', '=', self.company_id.id), ('acc_number', '=', self.account_number)])
        if not bank_account:
            bank_account = self.env['res.partner.bank'].create({
                'acc_number': self.account_number,
                'partner_id': self.partner_id.id,
                'company_id': self.company_id.id,
            })
        return bank_account

    def button_undo_reconciliation(self):
        ''' Undo the reconciliation mades on the statement line and reset their journal items
        to their original states.
        '''
        self.line_ids.remove_move_reconcile()
        self.payment_ids.unlink()

        for st_line in self:
            st_line.write({
                'to_check': False,
                'line_ids': [(5, 0)] + [(0, 0, line_vals) for line_vals in st_line._prepare_move_line_default_vals()],
            })
