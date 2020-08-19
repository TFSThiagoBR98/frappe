// Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and Contributors
// MIT License. See license.txt



frappe.ui.form.Review = class Review {
	constructor({parent, frm}) {
		this.parent = parent;
		this.frm = frm;
		this.points = frappe.boot.points;
		this.reviews = this.parent.find('.reviews');
		this.add_review_button();
		this.update_reviewers();
	}
	update_points() {
		return frappe.xcall('frappe.social.doctype.energy_point_log.energy_point_log.get_energy_points', {
			user: frappe.session.user
		}).then(data => {
			frappe.boot.points = data;
			this.points = data;
		});
	}
	add_review_button() {
		const review_button = this.parent.find('.add-review-btn');

		if (!this.points.review_points) {
			review_button.click(false);
			review_button.popover({
				trigger: 'hover',
				content: () => {
					return `<div class="text-medium">
						${__('You do not have enough review points')}
					</div>`;
				},
				html: true
			});
		} else {
			review_button.click(() => this.show_review_dialog());
		}
	}
	get_involved_users() {
		const user_fields = this.frm.meta.fields
			.filter(d => d.fieldtype === 'Link' && d.options === 'User')
			.map(d => d.fieldname);

		user_fields.push('owner');
		let involved_users = user_fields.map(field => this.frm.doc[field]);

		const docinfo = this.frm.get_docinfo();

		involved_users = involved_users.concat(
			docinfo.communications.map(d => d.sender && d.delivery_status==='sent'),
			docinfo.comments.map(d => d.owner),
			docinfo.versions.map(d => d.owner),
			docinfo.assignments.map(d => d.owner)
		);

		return involved_users
			.uniqBy(u => u)
			.filter(user => !['Administrator', frappe.session.user].includes(user))
			.filter(Boolean);
	}
	show_review_dialog() {
		const user_options = this.get_involved_users();
		const review_dialog = new frappe.ui.Dialog({
			'title': __('Add Review'),
			'fields': [{
				fieldname: 'to_user',
				fieldtype: 'Autocomplete',
				label: __('To User'),
				reqd: 1,
				options: user_options,
				ignore_validation: 1,
				description: __('Only users involved in the document are listed')
			}, {
				fieldname: 'review_type',
				fieldtype: 'Select',
				label: __('Action'),
				options: [{
					'label': __('Appreciate'),
					'value': 'Appreciation'
				}, {
					'label': __('Criticize'),
					'value': 'Criticism'
				}],
				default: 'Appreciation'
			}, {
				fieldname: 'points',
				fieldtype: 'Int',
				label: __('Points'),
				reqd: 1,
				description: __(`Currently you have ${this.points.review_points} review points`)
			}, {
				fieldtype: 'Small Text',
				fieldname: 'reason',
				reqd: 1,
				label: __('Reason')
			}],
			primary_action: (values) => {
				review_dialog.disable_primary_action();
				if (values.points > this.points.review_points) {
					return frappe.msgprint(__('You do not have enough points'));
				}
				frappe.xcall('frappe.social.doctype.energy_point_log.energy_point_log.review', {
					doc: {
						doctype: this.frm.doc.doctype,
						name: this.frm.doc.name,
					},
					to_user: values.to_user,
					points: values.points,
					review_type: values.review_type,
					reason: values.reason
				}).then(review => {
					review_dialog.hide();
					review_dialog.clear();
					this.frm.get_docinfo().energy_point_logs.unshift(review);
					this.frm.timeline.refresh();
					this.update_reviewers();
					this.update_points();
				}).finally(() => {
					review_dialog.enable_primary_action();
				});
			},
			primary_action_label: __('Submit')
		});
		review_dialog.show();
	}
	update_reviewers() {
		const review_logs = this.frm.get_docinfo().energy_point_logs
			.filter(log => ['Appreciation', 'Criticism'].includes(log.type));

		this.reviews.empty();
		review_logs.forEach(log => {
			let review_pill = $(`
				<li class="review ${log.points < 0 ? 'criticism': 'appreciation'}">
					<div>
						${Math.abs(log.points)}
					</div>
				</li>
			`);
			this.reviews.append(review_pill);
			this.setup_detail_popover(review_pill, log);
		});
	}
	setup_detail_popover(el, data) {
		let subject = '';
		let fullname = frappe.user.full_name(data.user);
		let timestamp = `<span class="text-muted">${frappe.datetime.comment_when(data.creation)}</span>`;
		let message_parts = [Math.abs(data.points), fullname, timestamp];
		if (data.type === 'Appreciation') {
			if (data.points == 1) {
				subject = __('{0} appreciation point for {1}', message_parts);
			} else {
				subject = __('{0} appreciation points for {1}', message_parts);
			}
		} else {
			if (data.points == -1) {
				subject = __('{0} criticism point for {1}', message_parts);
			} else {
				subject = __('{0} criticism points for {1}', message_parts);
			}
		}
		el.popover({
			animation: true,
			trigger: 'hover',
			delay: 500,
			placement: 'top',
			template: `
				<div class="popover" role="tooltip">
					<div class="arrow"></div>
					<h3 class="popover-header"></h3>
					<div class="popover-body">
					</div>
				</div>'
			`,
			content: () => {
				return `
					<div class="text-medium">
						<div class="subject">
							${frappe.utils.icon('review')}
							${subject}
						</div>
						<p>${__('By {0}', [frappe.user.full_name(data.owner)])} - ${timestamp}</p>
						<hr>
						<div class="body">
							<div>${data.reason}</div>
						</div>
					</div>
				`;
			},
			html: true,
			container: 'body'
		});
		return el;
	}
};
