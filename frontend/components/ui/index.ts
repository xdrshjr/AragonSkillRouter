/**
 * Reusable UI primitives. Everything below is typed, accessible and safe to
 * compose from new pages (SKILLs, Access, Records, Settings, agent panel).
 * See frontend/README.md for the contract of each primitive.
 */
export { Button, IconButton, type ButtonProps, type ButtonSize, type ButtonVariant } from './button';
export { Input, Textarea, type InputProps, type TextareaProps } from './input';
export { Select, type SelectOption, type SelectProps } from './select';
export { Checkbox, Switch, type CheckboxProps, type SwitchProps } from './checkbox';
export { Field, FormRow, FormSection, fieldIds, type FieldProps, type FormRowProps } from './field';
export { Badge, StatusBadge, type BadgeProps, type StatusBadgeProps } from './badge';
export { Card, CardBody, CardFooter, CardHeader } from './card';
export {
  Table,
  THead,
  TBody,
  TR,
  TH,
  TD,
  SortableTH,
  TableEmpty,
  type SortDirection,
  type SortState,
} from './table';
export { Dialog, type DialogProps, type DialogSize } from './dialog';
export { ConfirmDialog, type ConfirmDialogProps } from './confirm-dialog';
export { Drawer, type DrawerProps } from './drawer';
export { Tabs, TabPanel, type TabItem, type TabsProps } from './tabs';
export { Tooltip, type TooltipProps } from './tooltip';
export { ToastProvider, useToast, type ToastApi, type ToastInput, type ToastTone } from './toast';
export { Banner, InlineAlert, type BannerProps, type BannerTone } from './banner';
export { EmptyState, type EmptyStateProps } from './empty-state';
export { Skeleton, SkeletonBlock, SkeletonStats, SkeletonTable } from './skeleton';
export { CodeBlock, prettyJson, type CodeBlockProps } from './code-block';
export { SearchInput, type SearchInputProps } from './search-input';
export { Pagination, type PaginationProps } from './pagination';
export { PageHeader, SectionHeading, type PageHeaderProps } from './page-header';
export { Stat, StatGrid, type StatProps } from './stat';
export { Stepper, type Step, type StepStatus, type StepperProps } from './stepper';
export { JsonEditor, type JsonEditorProps } from './json-editor';
export { TimeAgo, type TimeAgoProps } from './time-ago';
